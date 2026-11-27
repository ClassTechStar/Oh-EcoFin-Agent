"""G1 适当性闸 / G2 资金与仓位闸（确定性代码，LLM 无权绕过）。

G1（下单前）：风险等级 × 权益上限、单一标的上限、行业上限；R1 禁止配置股票。
G2（下单前）：整手、可用资金、单笔上限、总仓位上限、当日亏损熔断、T+1 可用持仓。

设计纪律：
- 只减不增：任何调整都只能把数量往下调（adjusted_qty ≤ qty）；
- 超限但仍有可执行数量 → warn + 减量；减到 0 → block（理由必须可读）；
- 未知风险等级/非法配置 → 直接 block 或 ValueError，绝不"默认放行"。
"""

from __future__ import annotations

import logging
import math

from backend.execution.models import (
    COMMISSION_RATE,
    LOT_SIZE,
    AccountContext,
    OrderRequest,
    RiskDecision,
    estimate_fees,
    lot_floor,
)
from backend.quant.compliance import EQUITY_CAPS, SINGLE_CAPS, is_equity

logger = logging.getLogger(__name__)


class SuitabilityGate:
    """G1 适当性闸。"""

    name = "G1_suitability"

    def __init__(self, equity_caps: dict[str, float] | None = None,
                 single_caps: dict[str, float] | None = None):
        self.equity_caps = dict(equity_caps or EQUITY_CAPS)
        self.single_caps = dict(single_caps or SINGLE_CAPS)

    def pre_trade(self, order: OrderRequest, ctx: AccountContext) -> RiskDecision:
        if order.side == "sell":
            return RiskDecision(True, order.qty, [], "pass", self.name)
        if not is_equity(order.symbol):
            return RiskDecision(True, order.qty, [], "pass", self.name)

        equity_cap = self.equity_caps.get(ctx.risk_level)
        single_cap = self.single_caps.get(ctx.risk_level)
        if equity_cap is None or single_cap is None:
            return RiskDecision(False, 0, [f"未知风险等级: {ctx.risk_level}"],
                                "block", self.name)
        if equity_cap <= 0:
            return RiskDecision(False, 0,
                                [f"适当性闸：{ctx.risk_level} 等级禁止配置股票"],
                                "block", self.name)

        reasons: list[str] = []
        qty = order.qty
        equity_value = ctx.equity_value()
        room = equity_cap * ctx.total_equity - equity_value
        by_equity = lot_floor(room / order.price, LOT_SIZE) if room > 0 else 0
        if by_equity < qty:
            reasons.append(
                f"股票仓位 {equity_value / ctx.total_equity:.1%} 接近 "
                f"{ctx.risk_level} 上限 {equity_cap:.0%}")
            qty = by_equity

        pos = ctx.position(order.symbol)
        single_room = single_cap * ctx.total_equity - pos.qty * order.price
        by_single = lot_floor(single_room / order.price, LOT_SIZE) if single_room > 0 else 0
        if by_single < qty:
            reasons.append(
                f"单一标的 {order.symbol} 占比接近上限 {single_cap:.0%}")
            qty = by_single

        industry = ctx.industry.get(order.symbol)
        if industry and ctx.industry_caps:
            cap = ctx.industry_caps.get(industry)
            if cap is None:
                return RiskDecision(False, 0, [f"行业 {industry!r} 缺少敞口上限配置"],
                                    "block", self.name)
            current = sum(
                p.qty * order.price for s, p in ctx.positions.items()
                if ctx.industry.get(s) == industry)
            ind_room = cap * ctx.total_equity - current
            by_industry = lot_floor(ind_room / order.price, LOT_SIZE) if ind_room > 0 else 0
            if by_industry < qty:
                reasons.append(f"行业 {industry} 敞口接近上限 {cap:.0%}")
                qty = by_industry

        qty = max(int(qty), 0)
        if qty <= 0:
            reasons.append("适当性闸：无剩余可配置额度")
            return RiskDecision(False, 0, reasons, "block", self.name)
        severity = "pass" if qty == order.qty else "warn"
        if severity == "warn":
            reasons.append(f"适当性闸：数量 {order.qty}→{qty}")
        return RiskDecision(True, qty, reasons, severity, self.name)


class CapitalGate:
    """G2 资金与仓位闸。"""

    name = "G2_capital"

    def __init__(self, max_order_notional: float = 200_000.0,
                 max_total_exposure: float = 0.95):
        if not math.isfinite(max_order_notional) or max_order_notional <= 0:
            raise ValueError("max_order_notional 必须为有限正数")
        if not 0 < max_total_exposure <= 1:
            raise ValueError("max_total_exposure 必须在 (0, 1] 内")
        self.max_order_notional = float(max_order_notional)
        self.max_total_exposure = float(max_total_exposure)

    def pre_trade(self, order: OrderRequest, ctx: AccountContext) -> RiskDecision:
        if order.side == "sell":
            return self._sell(order, ctx)
        return self._buy(order, ctx)

    def _sell(self, order: OrderRequest, ctx: AccountContext) -> RiskDecision:
        available = ctx.position(order.symbol).available_qty
        if available <= 0:
            return RiskDecision(False, 0,
                                [f"T+1 可用持仓不足: {order.symbol}=0（当日买入不可卖）"],
                                "block", self.name)
        qty = min(order.qty, available)
        reasons = []
        if qty < order.qty:
            reasons.append(f"T+1 可用持仓 {available}，数量 {order.qty}→{qty}")
        return RiskDecision(True, qty, reasons,
                            "pass" if qty == order.qty else "warn", self.name)

    def _buy(self, order: OrderRequest, ctx: AccountContext) -> RiskDecision:
        if ctx.daily_pnl <= -abs(ctx.daily_loss_limit) * ctx.total_equity:
            return RiskDecision(
                False, 0,
                [f"当日亏损 {ctx.daily_pnl:.2f} 触及熔断阈值 "
                 f"{ctx.daily_loss_limit:.0%}，禁止买入"],
                "block", self.name)

        reasons: list[str] = []
        qty = lot_floor(order.qty, LOT_SIZE)
        if qty <= 0:
            return RiskDecision(False, 0, [f"不足一手（{LOT_SIZE} 股）"],
                                "block", self.name)
        if qty < order.qty:
            reasons.append(f"整手规则：数量 {order.qty}→{qty}")

        by_notional = lot_floor(self.max_order_notional / order.price, LOT_SIZE)
        if by_notional < qty:
            reasons.append(f"单笔金额上限 {self.max_order_notional:.0f} 元")
            qty = by_notional

        exposure = ctx.equity_value()
        room = self.max_total_exposure * ctx.total_equity - exposure
        by_exposure = lot_floor(room / order.price, LOT_SIZE) if room > 0 else 0
        if by_exposure < qty:
            reasons.append(f"总仓位接近上限 {self.max_total_exposure:.0%}")
            qty = by_exposure

        approx = int(ctx.cash / (order.price * (1 + COMMISSION_RATE)))
        qty = min(qty, lot_floor(approx, LOT_SIZE))
        while qty > 0 and order.price * qty + estimate_fees(
                "buy", order.price, qty) > ctx.cash:
            qty -= LOT_SIZE
        if qty <= 0:
            reasons.append("可用资金不足（含佣金）")
            return RiskDecision(False, 0, reasons, "block", self.name)

        if qty < order.qty and not any("数量" in r for r in reasons):
            reasons.append(f"资金/仓位闸：数量 {order.qty}→{qty}")
        return RiskDecision(True, qty, reasons,
                            "pass" if qty == order.qty else "warn", self.name)


class GateChain:
    """顺序执行闸门；任一 block 立即否决，调整量逐闸递减。"""

    def __init__(self, gates: list):
        if not gates:
            raise ValueError("GateChain 至少需要一个闸门")
        self.gates = list(gates)

    def evaluate(self, order: OrderRequest, ctx: AccountContext) -> RiskDecision:
        qty = order.qty
        reasons: list[str] = []
        severity = "pass"
        for gate in self.gates:
            decision = gate.pre_trade(order.model_copy(update={"qty": qty}), ctx)
            if decision.adjusted_qty > qty:
                raise ValueError(f"闸门 {gate.name} 试图加仓: {qty}→{decision.adjusted_qty}")
            reasons.extend(decision.reasons)
            if not decision.allowed or decision.adjusted_qty <= 0:
                return RiskDecision(False, 0, reasons, "block",
                                    gate.name)
            if decision.adjusted_qty < qty or decision.severity == "warn":
                severity = "warn"
                qty = decision.adjusted_qty
        return RiskDecision(True, qty, reasons, severity, "chain")
