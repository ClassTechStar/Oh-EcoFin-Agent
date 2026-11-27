"""数据源工厂与快照兼容适配。

`spot_to_legacy` 说明：快照在内部（SQLite/工具/测试）仍使用 v1 中文键，
Provider 层输出规范英文列；本适配器是两套 schema 之间的**唯一**转换点。
后续 M2 迁移快照 schema 时只需删除本函数与调用处。
"""

from __future__ import annotations

import pandas as pd

from backend.config import Settings
from backend.data.providers.akshare_provider import AkshareProvider
from backend.data.providers.base import (MarketDataProvider, ProviderUnavailable,
                                         Quote, SPOT_COLUMNS, PRICE_COLUMNS)
from backend.data.providers.offline_provider import OfflineProvider
from backend.data.ratelimit import TokenBucket

#: 规范列 → 内部快照 v1 列（迁移期唯一映射点）
_LEGACY_SPOT_MAP = {
    "symbol": "代码", "name": "名称", "price": "最新价", "pct_chg": "涨跌幅",
    "volume": "成交量", "amount": "成交额", "turnover": "换手率",
    "volume_ratio": "量比", "market_cap": "总市值", "pe": "市盈率-动态", "pb": "市净率",
}


def build_provider(settings: Settings) -> MarketDataProvider:
    """按配置构建数据源：DEMO_MODE → 离线卡带；否则 AKShare（东财→新浪）。"""
    if settings.app.demo_mode:
        return OfflineProvider(settings.cassette_dir)
    return AkshareProvider(TokenBucket(settings.data.rate_limit_rps,
                                       settings.data.rate_limit_burst))


def spot_to_legacy(df: pd.DataFrame) -> pd.DataFrame:
    """规范快照列 → 内部 v1 中文列（保留停牌/涨跌停质检的既有字段语义）。"""
    out = df.rename(columns=_LEGACY_SPOT_MAP).copy()
    out["代码"] = out["代码"].astype(str).str.zfill(6)
    return out


__all__ = [
    "MarketDataProvider", "ProviderUnavailable", "Quote",
    "SPOT_COLUMNS", "PRICE_COLUMNS",
    "AkshareProvider", "OfflineProvider", "build_provider", "spot_to_legacy",
]
