"""行情数据源抽象（M1）。

设计来源：FinancialAI-Agent `providers/base.py:30-90` 的 Provider + CAPABILITIES 模式。

三条契约：
1. **列名与源无关**：日线统一 `date/open/high/low/close/volume/amount`，快照统一
   `symbol/name/price/pct_chg/...`；禁止让中文列名下沉到上层。
2. **能力声明 + 快速失败**：`CAPABILITIES` 声明支持的能力，不支持时抛
   `ProviderUnavailable(hint=...)`，让上层能给出可执行提示而不是空数据。
3. **离线优先可验证**：`OfflineProvider` 与真实 Provider 同契约，CI 与演示
   可在零网络、零密钥下验证完整工具面。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

#: 日线规范列（升序，date 为 YYYY-MM-DD 字符串）
PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

#: 全市场快照规范列
SPOT_COLUMNS = [
    "symbol", "name", "price", "pct_chg", "volume", "amount",
    "turnover", "volume_ratio", "market_cap", "pe", "pb", "ts",
]


class ProviderUnavailable(Exception):
    """数据源不可用或能力不支持（附可执行提示）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Quote:
    """标准化快照（供信号/风控层直接消费，避免字典键漂移）。"""

    symbol: str
    name: str
    price: float
    pre_close: float = 0.0
    pct_chg: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    turnover: float = 0.0
    volume_ratio: float = 0.0
    market_cap: float = 0.0
    pe: float = 0.0
    pb: float = 0.0
    ts: str = ""
    source: str = ""
    stale: bool = False


class MarketDataProvider(ABC):
    """只读行情数据门面。"""

    name: str = "base"
    #: 能力标签：spot_all / daily_bar / financial / valuation
    CAPABILITIES: frozenset[str] = frozenset()
    #: 最近一次取数的实际来源（多源降级时用于诚实标注）
    last_source: str = "base"

    def supports(self, capability: str) -> bool:
        return capability in self.CAPABILITIES

    def require(self, capability: str) -> None:
        if not self.supports(capability):
            raise ProviderUnavailable(
                f"数据源 '{self.name}' 不支持 '{capability}'",
                hint="切换 FINAGENT_PROVIDER，或改用其他能力",
            )

    @abstractmethod
    def spot_all(self) -> pd.DataFrame:
        """全市场快照（SPOT_COLUMNS 规范列）。

        约定：`price` 为 0 或 `volume` 为 0 视为停牌，由质检层判定，不在 Provider 内过滤。
        """

    @abstractmethod
    def daily_bar(self, symbol: str, days: int = 120, adjust: str = "hfq") -> pd.DataFrame:
        """日线（PRICE_COLUMNS 规范列，按 date 升序）。"""

    def financial_indicators(self, symbol: str) -> list[dict]:
        """主要财务指标（按报告期倒序）。不支持时抛 ProviderUnavailable。"""
        self.require("financial")
        return []


def normalize_daily(df: pd.DataFrame, rename: dict[str, str]) -> pd.DataFrame:
    """通用列名重映射 + 类型规整（AKShare 列名随版本漂移的唯一收口点）。"""
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    missing = [c for c in ("date", "open", "high", "low", "close") if c not in out.columns]
    if missing:
        raise ProviderUnavailable(
            f"日线缺关键列: {missing}",
            hint="AKShare 列名可能已漂移，请更新 providers 的重映射层并锁版本",
        )
    out["date"] = out["date"].astype(str).str.slice(0, 10)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    if "amount" not in out.columns:
        out["amount"] = 0.0
    return out[PRICE_COLUMNS]
