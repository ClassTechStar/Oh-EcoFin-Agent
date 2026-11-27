"""投研工具：财务/估值/预计算指标/结构化报告提交（溯源协议）。"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.data.ratelimit import TokenBucket

logger = logging.getLogger(__name__)


class SymbolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6位A股代码")

    @field_validator("symbol")
    @classmethod
    def _v(cls, v: str) -> str:
        v = str(v).strip().zfill(6)
        if not v.isdigit():
            raise ValueError(f"非法股票代码: {v}")
        return v


class CorrelationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(..., min_length=2, max_length=5,
                               description="对比标的列表（2-5 只）")

    @field_validator("symbols")
    @classmethod
    def _v(cls, v):
        out = [str(s).strip().zfill(6) for s in v]
        if any(not s.isdigit() for s in out):
            raise ValueError("非法股票代码")
        return out


class ReportSection(BaseModel):
    type: str = Field(..., description="text|table|sentiment|debate|chart_ref")
    title: str = ""
    text: str = ""
    chart_id: str = ""
    rows: list[list] = Field(default_factory=list)


class Citation(BaseModel):
    n: int
    tool: str
    args: str = ""
    ts: str
    excerpt: str = ""


class ReportMeta(BaseModel):
    symbol: str
    name: str = ""
    data_as_of: str
    generated_at: str = ""


class ResearchReport(BaseModel):
    """附录 A：投研报告 JSON Schema（submit_research_report 入参）。"""
    meta: ReportMeta
    sections: list[ReportSection] = Field(..., min_length=3, max_length=12)
    citations: list[Citation] = Field(..., min_length=4)
    verdict: str = Field("neutral", description="bullish|bearish|neutral|watch")
    thesis_digest: str = Field("", description="结论一句话摘要")
    risk_disclosure: str = Field(..., min_length=40)

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v: str) -> str:
        if v not in {"bullish", "bearish", "neutral", "watch"}:
            raise ValueError("verdict 必须是 bullish/bearish/neutral/watch")
        return v


NUM_RE = re.compile(r"\[(\d+)\]")


def cross_check_citations(text: str, citations: list[Citation]) -> list[str]:
    """正文 [n] 与 citations 编号 1:1 交叉校验：返回问题列表（空 = 通过）。"""
    in_text = {int(m) for m in NUM_RE.findall(text or "")}
    declared = {c.n for c in citations}
    issues = []
    for n in sorted(in_text - declared):
        issues.append(f"正文引用 [{n}] 未在 citations 中声明")
    for n in sorted(declared - in_text):
        issues.append(f"citations 中的 [{n}] 未在正文使用")
    return issues


class ResearchTools:
    def __init__(self, market, search_service, precompute, bucket: TokenBucket | None = None):
        self.market = market
        self.search = search_service
        self.precompute = precompute
        self.bucket = bucket or TokenBucket()
        self._fin_cache: dict[str, tuple[float, dict]] = {}
        self.fin_cache_ttl = 3600

    # ---------- get_financial ----------

    async def get_financial(self, symbol: str) -> dict:
        """主要财务指标（统一走 Provider，真实源与离线卡带同契约）。"""
        cached = self._fin_cache.get(symbol)
        if cached and time.time() - cached[0] < self.fin_cache_ttl:
            return {"ok": True, "symbol": symbol, **cached[1], "cached": True}
        try:
            if not self.market.demo_mode:
                await self.bucket.acquire()
            indicators = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.market.provider.financial_indicators(symbol))
        except Exception as e:
            return {"ok": False, "reason": f"财务数据获取失败: {type(e).__name__}: {e}"}
        data = {"indicators": indicators}
        self._fin_cache[symbol] = (time.time(), data)
        return {"ok": bool(indicators), "symbol": symbol, **data}

    # ---------- get_valuation ----------

    async def get_valuation(self, symbol: str) -> dict:
        """PE/PB/PS 近 3 年分位（快照现值 + K 线派生近似分位）。"""
        try:
            df = await self.market.get_kline_raw(symbol, 500)
        except Exception as e:
            return {"ok": False, "reason": f"K线获取失败: {type(e).__name__}"}
        row = self.market.snapshot.row(symbol)
        if row is None:
            return {"ok": False, "reason": "快照无此标的"}
        pe_now, pb_now = row.get("市盈率-动态"), row.get("市净率")
        out: dict = {"ok": True, "symbol": symbol,
                     "pe": pe_now, "pb": pb_now,
                     "name": row.get("名称", "")}
        # 近 3 年收盘价分位（价格分位作为估值锚的近似口径，演示用）
        close = df["close"].astype(float).tail(731)
        if len(close) >= 120:
            pct = float((close < float(close.iloc[-1])).mean() * 100)
            out["price_percentile_3y"] = round(pct, 1)
            out["note"] = "价格分位为演示口径；PE/PB 分位需历史估值序列（AKShare 个股指标）"
        return out

    # ---------- get_indicator ----------

    async def get_indicator(self, symbol: str) -> dict:
        """预计算层命中：零计算零等待。"""
        ind = self.precompute.get(symbol)
        if ind is None:
            # 未预计算 → 一次后台计算（关注池自愈）
            try:
                df = await self.market.get_kline_raw(symbol, 120)
                ind = {}
                from backend.data.precompute import compute_indicators

                ind = compute_indicators(df)
                self.precompute.update(symbol, ind)
            except Exception as e:
                return {"ok": False, "reason": f"指标计算失败: {type(e).__name__}"}
        return {"ok": bool(ind), "symbol": symbol, "indicator": ind,
                "data_as_of": self.market.snapshot.as_of}

    # ---------- get_correlation ----------

    async def get_correlation(self, symbols: list[str]) -> dict:
        """多股对比：近 120 日后复权收盘价 Pearson 相关性矩阵（热力图富组件）。"""
        if len(symbols) < 2:
            return {"ok": False, "reason": "相关性需要 ≥2 只标的"}
        import pandas as pd  # noqa: PLC0415

        frames = {}
        for sym in symbols[:5]:
            try:
                df = await self.market.get_kline_raw(sym, 120)
                frames[sym] = df.set_index("date")["close"].astype(float)
            except Exception as e:
                return {"ok": False, "reason": f"{sym} K线获取失败: {type(e).__name__}"}
        prices = pd.DataFrame(frames).dropna()
        if prices.shape[0] < 60:
            return {"ok": False, "reason": "共同交易日不足 60 天"}
        corr = prices.pct_change().corr()
        return {
            "ok": True,
            "matrix": {
                "symbols": list(corr.columns),
                "values": [[round(float(x), 3) for x in row] for row in corr.values],
            },
            "period_days": int(prices.shape[0]),
        }

    # ---------- submit_research_report ----------

    async def submit_research_report(self, meta: dict, sections: list[dict],
                                     citations: list[dict], verdict: str = "neutral",
                                     thesis_digest: str = "", risk_disclosure: str = "",
                                     **_) -> dict:
        """结构化输出：Function Calling 强约束 JSON；[n] 与 citations 交叉校验。

        入参为报告 Schema 的顶层字段（LLM 按 tools Schema 直接给出）。
        """
        try:
            report = ResearchReport(
                meta=ReportMeta(**meta),
                sections=[ReportSection(**s) for s in sections],
                citations=[Citation(**c) for c in citations],
                verdict=verdict, thesis_digest=thesis_digest,
                risk_disclosure=risk_disclosure)
        except Exception as e:
            return {"ok": False, "error": f"报告 Schema 校验失败: {e}",
                    "hint": "请按 submit_research_report 的 Schema 重新提交"}
        all_text = " ".join(s.text for s in report.sections)
        issues = cross_check_citations(all_text, report.citations)
        if issues:
            return {"ok": False, "error": "引用编号交叉校验失败", "issues": issues,
                    "hint": "请修正正文 [n] 与 citations 的对应关系后重新提交"}
        if not report.risk_disclosure or len(report.risk_disclosure) < 40:
            return {"ok": False, "error": "缺少合规免责声明", "hint": "risk_disclosure ≥40 字"}
        return {"ok": True, "report": report.model_dump()}
