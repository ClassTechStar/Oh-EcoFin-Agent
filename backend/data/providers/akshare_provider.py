"""AKShare Provider（东财主源 → 新浪降级）。

列名重映射的唯一收口点：AKShare 列名随版本漂移，必须锁版本 + 在此归一，
上层只认 `PRICE_COLUMNS` / `SPOT_COLUMNS`。
"""

from __future__ import annotations

import logging

import pandas as pd

from backend.data.providers.base import (MarketDataProvider, ProviderUnavailable,
                                         SPOT_COLUMNS, normalize_daily)
from backend.data.ratelimit import TokenBucket

logger = logging.getLogger(__name__)

# 东财全市场快照列 → 规范列
_EM_SPOT_MAP = {
    "代码": "symbol", "名称": "name", "最新价": "price", "涨跌幅": "pct_chg",
    "成交量": "volume", "成交额": "amount", "换手率": "turnover",
    "量比": "volume_ratio", "总市值": "market_cap",
    "市盈率-动态": "pe", "市净率": "pb",
}

# 新浪快照列 → 规范列（新浪代码带 sh/sz 前缀，需去前缀）
_SINA_SPOT_MAP = {
    "代码": "symbol", "名称": "name", "最新价": "price", "涨跌幅": "pct_chg",
    "成交量": "volume", "成交额": "amount", "换手率": "turnover",
    "量比": "volume_ratio", "总市值": "market_cap",
    "市盈率": "pe", "市净率": "pb",
}

# 日线列 → 规范列
_DAILY_MAP = {
    "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
    "收盘": "close", "成交量": "volume", "成交额": "amount",
}


class AkshareProvider(MarketDataProvider):
    """生产数据源：东财优先，失败自动切新浪。"""

    name = "akshare"
    CAPABILITIES = frozenset({"spot_all", "daily_bar", "financial"})

    def __init__(self, bucket: TokenBucket | None = None):
        self.bucket = bucket or TokenBucket(1.0, 3)
        self.last_source = "akshare"

    # ---------- 快照 ----------

    def spot_all(self) -> pd.DataFrame:
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot_em()
            self.last_source = "eastmoney"
            return self._normalize_spot(df, _EM_SPOT_MAP)
        except Exception as e:
            logger.warning("东财快照失败，降级新浪: %s", e)
            try:
                import akshare as ak

                df = ak.stock_zh_a_spot()
                self.last_source = "sina"
                return self._normalize_spot(df, _SINA_SPOT_MAP)
            except Exception as e2:
                raise ProviderUnavailable(
                    f"东财与新浪快照均失败: {type(e2).__name__}",
                    hint="检查网络/代理，或启用 DEMO_MODE 使用冻结卡带",
                ) from e2

    def _normalize_spot(self, df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
        out = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns}).copy()
        if "symbol" not in out.columns:
            raise ProviderUnavailable("快照缺 symbol 列", hint="AKShare 接口变更，请更新映射表")
        out["symbol"] = (
            out["symbol"].astype(str)
            .str.replace(r"^(sh|sz|bj)", "", regex=True)
            .str.zfill(6)
        )
        for col in ("price", "pct_chg", "volume", "amount", "turnover",
                    "volume_ratio", "market_cap", "pe", "pb"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
            else:
                out[col] = float("nan")
        for col in ("name", "ts"):
            if col not in out.columns:
                out[col] = ""
        return out[SPOT_COLUMNS]

    # ---------- 日线 ----------

    def daily_bar(self, symbol: str, days: int = 120, adjust: str = "hfq") -> pd.DataFrame:
        try:
            import akshare as ak
        except ImportError as e:  # pragma: no cover - 环境问题
            raise ProviderUnavailable("未安装 akshare", hint="pip install -r requirements.txt") from e

        end = pd.Timestamp.now()
        start = end - pd.Timedelta(days=max(days * 2, 365))  # 预留非交易日
        try:
            df = ak.stock_zh_a_hist(
                symbol=str(symbol).zfill(6), period="daily",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                adjust=adjust,
            )
        except Exception as e:
            raise ProviderUnavailable(
                f"日线获取失败({symbol}): {type(e).__name__}",
                hint="可能触发限流或接口变更；稍后重试或切换数据源",
            ) from e
        self.last_source = "eastmoney"
        out = normalize_daily(df, _DAILY_MAP)
        return out.tail(days).reset_index(drop=True)

    # ---------- 财务 ----------

    def financial_indicators(self, symbol: str) -> list[dict]:
        try:
            import akshare as ak

            df = ak.stock_financial_abstract_ths(symbol=str(symbol).zfill(6),
                                                indicator="按报告期")
        except Exception as e:
            raise ProviderUnavailable(
                f"财务指标获取失败({symbol}): {type(e).__name__}",
                hint="该标的可能无财务数据或接口限流",
            ) from e
        if df is None or df.empty:
            return []
        keep = [c for c in df.columns
                if any(k in str(c) for k in ("报告期", "营业总收入", "营业收入", "净利润",
                                             "毛利率", "经营现金", "同比增长"))][:10]
        rows = df.head(4)[keep].to_dict(orient="records")
        return [{str(k): v for k, v in row.items()} for row in rows]
