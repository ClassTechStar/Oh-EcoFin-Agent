"""行情工具：get_quote（快照命中 <50ms）/ get_kline（Provider 日线 + TTL 缓存）。"""

from __future__ import annotations

import asyncio
import logging
import time

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.data.providers import AkshareProvider, OfflineProvider
from backend.data.quality import check_limit_hit, check_suspended, classify_board, freshness
from backend.data.ratelimit import TokenBucket

logger = logging.getLogger(__name__)


class QuoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(..., min_length=1, max_length=5,
                               description="6位A股代码列表，最多5只")
    names: dict[str, str] = Field(default_factory=dict, description="代码→名称映射（可选）")

    @field_validator("symbols")
    @classmethod
    def _validate_symbols(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = str(s).strip().zfill(6)
            if not s.isdigit() or len(s) != 6:
                raise ValueError(f"非法股票代码: {s}")
            out.append(s)
        return out


class KlineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6位A股代码")
    days: int = Field(120, ge=30, le=500, description="K线天数，范围[30,500]")
    adjust: str = Field("hfq", description="复权口径，回测一律 hfq 后复权")

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, v: str) -> str:
        v = str(v).strip().zfill(6)
        if not v.isdigit():
            raise ValueError(f"非法股票代码: {v}")
        return v


class MarketTools:
    """行情工具集；快照服务与令牌桶由 app 装配注入。"""

    def __init__(self, snapshot, bucket: TokenBucket | None = None,
                 kline_cache_ttl: int = 3600, cassette_dir: str = "",
                 demo_mode: bool = False, provider=None):
        self.snapshot = snapshot
        self.bucket = bucket or TokenBucket()
        self.kline_cache: dict[str, tuple[float, dict]] = {}
        self.kline_cache_ttl = kline_cache_ttl
        self.demo_mode = demo_mode
        self.cassette_dir = cassette_dir
        self.provider = provider or (
            OfflineProvider(cassette_dir) if demo_mode
            else AkshareProvider(self.bucket)
        )
        self._kline_df_cache: dict[str, pd.DataFrame] = {}

    # ---------- get_quote ----------

    async def get_quote(self, symbols: list[str], names: dict[str, str] | None = None) -> dict:
        """快照命中路径：无网络调用，<50ms。"""
        t0 = time.perf_counter()
        names = names or {}
        quotes = []
        for sym in symbols:
            row = self.snapshot.row(sym)
            if row is None:
                quotes.append({"symbol": sym, "ok": False, "reason": "快照无此标的"})
                continue
            if check_suspended(row):
                quotes.append({"symbol": sym, "ok": False, "reason": "停牌",
                               "name": row.get("名称", names.get(sym, ""))})
                continue
            pct = row.get("涨跌幅")
            quotes.append({
                "symbol": sym,
                "name": row.get("名称", names.get(sym, "")),
                "ok": True,
                "price": row.get("最新价"),
                "pct_chg": pct,
                "volume_ratio": row.get("量比"),
                "turnover": row.get("换手率"),
                "market_cap": row.get("总市值"),
                "pe": row.get("市盈率-动态"),
                "pb": row.get("市净率"),
                "limit_hit": check_limit_hit(sym, pct, row.get("名称", "")),
                "board": classify_board(sym, row.get("名称", "")),
                **freshness(self.snapshot.as_of, self.snapshot.freshness()["is_stale"]),
            })
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": all(q.get("ok") for q in quotes), "quotes": quotes,
                "elapsed_ms": elapsed_ms, "source": self.snapshot._source}

    # ---------- get_kline ----------

    async def get_kline_raw(self, symbol: str, days: int = 120, adjust: str = "hfq") -> pd.DataFrame:
        """原始 K 线 DataFrame（回测/因子复用；走令牌桶 + TTL 缓存）。

        取数统一走 Provider：真实源（东财→新浪）与离线卡带同契约，
        列名漂移只在 Provider 的归一化层处理。
        """
        cache_key = f"{symbol}:{adjust}"
        hit = self._kline_df_cache.get(cache_key)
        if hit is not None:
            df, ts = hit
            if time.time() - ts < self.kline_cache_ttl and len(df) >= min(days, 60):
                return df
        if not self.demo_mode:
            await self.bucket.acquire()
        df = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self.provider.daily_bar(symbol, days, adjust))
        self._kline_df_cache[cache_key] = (df, time.time())
        return df

    async def get_close_series(self, symbol: str, days: int = 30) -> pd.Series:
        """收盘价序列（T+N 决策结算用；index=date, values=close）。"""
        df = await self.get_kline_raw(symbol, days)
        return df.set_index("date")["close"].astype(float)

    async def get_kline(self, symbol: str, days: int = 120, adjust: str = "hfq") -> dict:
        df = await self.get_kline_raw(symbol, days, adjust)
        if df.empty:
            return {"ok": False, "reason": "K线数据为空"}
        bars = [
            {"date": str(r.get("date", ""))[:10], "open": float(r["open"]),
             "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]), "volume": float(r["volume"])}
            for _, r in df.tail(days).iterrows()
            if pd.notna(r.get("close"))
        ]
        return {"ok": True, "symbol": symbol, "adjust": adjust, "bars": bars,
                **freshness(self.snapshot.as_of, True)}


def build_market_tools(snapshot, settings) -> MarketTools:
    return MarketTools(
        snapshot=snapshot,
        bucket=TokenBucket(settings.data.rate_limit_rps, settings.data.rate_limit_burst),
        kline_cache_ttl=settings.data.kline_cache_ttl,
        cassette_dir=settings.cassette_dir,
        demo_mode=settings.app.demo_mode,
    )
