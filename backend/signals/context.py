"""`DataContext` 的行情实现：只读点位视图，end_date 之后的数据一律不可见。

回测/复现场景由调用方传 `end_date` 切片；实盘研判用 `as_of` 固定时点。
这是「模型不可能偷看未来数据」的结构性保障（而非靠提示词自觉）。
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

DATE_COLUMNS = ("date", "日期", "时间")


def _cut(df: pd.DataFrame, end_date: str) -> pd.DataFrame:
    """按日期列切片（兼容 date/中文列），保留 ≤ end_date 的行。"""
    if df is None or df.empty:
        return df
    col = next((c for c in DATE_COLUMNS if c in df.columns), None)
    if col is None:
        raise ValueError(f"日线缺日期列（现有列: {list(df.columns)}）")
    cut = str(end_date)[:10]
    mask = df[col].astype(str).str.slice(0, 10) <= cut
    return df.loc[mask].reset_index(drop=True)


class MarketDataContext:
    """把 MarketTools 适配成 AlphaModel 的 `DataContext`。"""

    def __init__(self, market, as_of: str | None = None):
        self.market = market
        self.as_of = as_of

    async def daily_bars(self, symbol: str, days: int = 120,
                         end_date: str | None = None) -> pd.DataFrame:
        df = await self.market.get_kline_raw(symbol, days, "hfq")
        cut = (end_date or self.as_of or "")[:10]
        return _cut(df, cut) if cut else df

    async def quote(self, symbol: str) -> dict:
        return await self.market.get_quote(symbol)
