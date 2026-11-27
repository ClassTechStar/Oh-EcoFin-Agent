"""预计算指标层：关注池在快照刷新后增量计算，工具调用时零计算零等待。

指标：MA5/10/20/60、RSI14、MACD(12,26,9)、量比、换手。
pandas-ta 只在后台线程跑，工具热路径上没有任何指标计算。
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def compute_indicators(df: pd.DataFrame) -> dict:
    """对单标的日线 DataFrame 计算技术指标（纯函数，可单测）。

    输入列：date/close/volume（后复权口径）；输出最新指标快照 dict。
    """
    if df is None or df.empty:
        return {}
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    vol = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0)
    if close.empty:
        return {}
    out: dict = {"close": round(float(close.iloc[-1]), 3), "bars": len(close)}
    for n in (5, 10, 20, 60):
        if len(close) >= n:
            out[f"ma{n}"] = round(float(close.tail(n).mean()), 3)
    # RSI14（Wilder 平滑）
    if len(close) >= 15:
        try:
            import pandas_ta_classic as ta  # noqa: PLC0415

            rsi = ta.rsi(close, length=14)
            out["rsi14"] = round(float(rsi.iloc[-1]), 2)
        except Exception:
            out["rsi14"] = _rsi_fallback(close)
    else:
        out["rsi14"] = _rsi_fallback(close) if len(close) >= 3 else None
    # MACD(12,26,9)
    if len(close) >= 35:
        try:
            import pandas_ta_classic as ta  # noqa: PLC0415

            macd = ta.macd(close, fast=12, slow=26, signal=9)
            out["macd"] = round(float(macd.iloc[-1, 0]), 4)
            out["macd_signal"] = round(float(macd.iloc[-1, 1]), 4)
            out["macd_hist"] = round(float(macd.iloc[-1, 2]), 4)
        except Exception:
            out.update(_macd_fallback(close))
    # 量比：近 5 日均量 / 近 60 日均量
    if len(vol) >= 60:
        v5, v60 = float(vol.tail(5).mean()), float(vol.tail(60).mean())
        out["volume_ratio"] = round(v5 / v60, 2) if v60 else None
    return out


def _rsi_fallback(close: pd.Series, length: int = 14) -> float | None:
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    if loss.iloc[-1] == 0:
        return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return round(100 - 100 / (1 + rs), 2)


def _macd_fallback(close: pd.Series) -> dict:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return {"macd": round(float(dif.iloc[-1]), 4),
            "macd_signal": round(float(dea.iloc[-1]), 4),
            "macd_hist": round(float(dif.iloc[-1] - dea.iloc[-1]), 4)}


class PrecomputeService:
    """关注池指标缓存：watchlist ∪ 决策日志标的 ∪ 演示白名单。"""

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._kline_fetcher = None  # async (symbol, days) -> DataFrame

    def set_fetcher(self, fn) -> None:
        self._kline_fetcher = fn

    def get(self, symbol: str) -> dict | None:
        return self._cache.get(symbol)

    def update(self, symbol: str, indicators: dict) -> None:
        self._cache[symbol] = indicators

    async def precompute(self, symbols: list[str], days: int = 120) -> dict[str, dict]:
        """后台批量预计算（快照刷新后调用）。"""
        if self._kline_fetcher is None:
            return self._cache
        results = {}
        for sym in symbols:
            try:
                df = await self._kline_fetcher(sym, days)
                results[sym] = compute_indicators(df)
            except Exception:
                logger.warning("预计算失败: %s", sym, exc_info=True)
        self._cache.update(results)
        return results
