"""执行层数据契约（M10）：订单 / 账户 / 风控决策。

规则与报告 5.4.6 对齐：
- `RiskDecision` 只有三种严重度：pass / warn / block；
- 数量调整必须是**减量**（adjusted_qty ≤ qty），不允许闸门偷偷加仓；
- 账户上下文显式携带风险等级、行业映射、当日盈亏——风控不靠全局状态。
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
Severity = Literal["pass", "warn", "block"]

#: 与回测同一套 A 股摩擦参数（单一事实源）
COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
STAMP_DUTY = 0.0005
LOT_SIZE = 100

SYMBOL_RE = re.compile(r"^\d{6}$")


class OrderRequest(BaseModel):
    """下单请求（qty 为正整数股数；市价单可不带 limit_price）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"ord-{uuid.uuid4().hex[:12]}")
    strategy_id: str = "manual"
    symbol: str
    side: OrderSide
    qty: int = Field(..., ge=1, description="委托股数（买侧须为整手）")
    order_type: OrderType = "market"
    price: float = Field(..., gt=0, description="决策参考价（快照/盘口）")
    limit_price: float | None = Field(None, gt=0)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = str(v).strip().zfill(6)
        if not SYMBOL_RE.match(v):
            raise ValueError(f"非法股票代码: {v}")
        return v

    @model_validator(mode="after")
    def _limit_requires_price(self) -> OrderRequest:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("限价单必须提供 limit_price")
        return self


@dataclass
class Position:
    symbol: str
    qty: int = 0
    available_qty: int = 0
    avg_cost: float = 0.0

    def market_value(self, price: float) -> float:
        return self.qty * float(price)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "qty": self.qty,
                "available_qty": self.available_qty, "avg_cost": self.avg_cost}


@dataclass
class AccountContext:
    """风控闸门的只读输入（每次评估显式传入，避免隐式全局状态）。"""

    cash: float
    total_equity: float
    risk_level: str = "R3"
    positions: dict[str, Position] = field(default_factory=dict)
    industry: dict[str, str] = field(default_factory=dict)
    industry_caps: dict[str, float] = field(default_factory=dict)
    daily_pnl: float = 0.0
    daily_loss_limit: float = 0.02

    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol) or Position(symbol)

    def equity_value(self, prices: dict[str, float] | None = None) -> float:
        out = 0.0
        for sym, pos in self.positions.items():
            if pos.qty <= 0:
                continue
            price = (prices or {}).get(sym, pos.avg_cost)
            out += pos.qty * float(price)
        return out


@dataclass
class RiskDecision:
    """单闸/闸链结论：allowed + 可执行数量 + 理由 + 严重度。"""

    allowed: bool
    adjusted_qty: int
    reasons: list[str] = field(default_factory=list)
    severity: Severity = "pass"
    gate: str = ""

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "adjusted_qty": self.adjusted_qty,
                "reasons": list(self.reasons), "severity": self.severity,
                "gate": self.gate}


class RiskGate(Protocol):
    name: str

    def pre_trade(self, order: OrderRequest,
                  ctx: AccountContext) -> RiskDecision: ...


def estimate_fees(side: OrderSide, price: float, qty: int, *,
                  commission_rate: float = COMMISSION_RATE,
                  min_commission: float = MIN_COMMISSION,
                  stamp_duty: float = STAMP_DUTY) -> float:
    """A 股费用：佣金双边（最低 5 元）+ 印花税仅卖侧。"""
    turnover = float(price) * int(qty)
    fee = max(turnover * commission_rate, min_commission)
    if side == "sell":
        fee += turnover * stamp_duty
    return round(fee, 2)


def lot_floor(qty: int, lot: int = LOT_SIZE) -> int:
    """整手向下取整（A 股买入最小单位）。"""
    return max(int(qty) // int(lot) * int(lot), 0)


def is_finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0
