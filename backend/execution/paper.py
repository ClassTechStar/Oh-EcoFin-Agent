"""PaperBroker（M10 仿真撮合）：A 股 T+1 / 整手 / 涨跌停 / 停牌 / 成本 + 每日对账。

撮合规则（报告 5.7）：
- 市价单以对手价 + 滑点成交；涨停不可买、跌停不可卖、停牌不成交；
- 限价单价格穿越才成交；
- T+1：当日买入进入待解冻，`roll_day()` 后才可卖；
- 每日对账恒等式：`现金 + 持仓市值 = 期初 + 已实现盈亏 − 费用`（浮点容差 0.01）。

费用口径与回测完全一致（佣金双边最低 5 元 + 印花税仅卖侧），避免"回测赚钱、仿真亏钱"的口径漂移。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from backend.execution.models import (
    COMMISSION_RATE,
    LOT_SIZE,
    MIN_COMMISSION,
    STAMP_DUTY,
    AccountContext,
    OrderRequest,
    Position,
    estimate_fees,
)

logger = logging.getLogger(__name__)

#: 对账恒等式容差（元）
RECONCILE_TOLERANCE = 0.01

#: 涨跌停判定容差（价格比较用分位）
PRICE_EPS = 0.005


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    commission: float
    stamp_duty: float
    ts: str = ""

    @property
    def fees(self) -> float:
        return round(self.commission + self.stamp_duty, 2)

    def to_dict(self) -> dict:
        return {"order_id": self.order_id, "symbol": self.symbol, "side": self.side,
                "qty": self.qty, "price": self.price, "commission": self.commission,
                "stamp_duty": self.stamp_duty, "fees": self.fees, "ts": self.ts}


class PaperBroker:
    """内存撮合器（同步；下单路由负责风控与落库）。"""

    def __init__(self, cash: float = 1_000_000.0, *,
                 commission_rate: float = COMMISSION_RATE,
                 min_commission: float = MIN_COMMISSION,
                 stamp_duty: float = STAMP_DUTY,
                 lot_size: int = LOT_SIZE,
                 slippage: float = 0.0):
        self.initial_cash = float(cash)
        self.cash = float(cash)
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_duty = float(stamp_duty)
        self.lot_size = int(lot_size)
        self.slippage = float(slippage)
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self._pending_buys: dict[str, int] = {}

    # ---------- 账户视图 ----------

    def market_value(self, prices: Mapping[str, float]) -> float:
        return round(sum(p.qty * float(prices.get(s, p.avg_cost))
                         for s, p in self.positions.items() if p.qty > 0), 2)

    def context(self, prices: Mapping[str, float], *,
                risk_level: str = "R3",
                industry: Mapping[str, str] | None = None,
                industry_caps: Mapping[str, float] | None = None,
                daily_pnl: float = 0.0,
                daily_loss_limit: float = 0.02) -> AccountContext:
        mv = self.market_value(prices)
        return AccountContext(
            cash=round(self.cash, 2),
            total_equity=round(self.cash + mv, 2),
            risk_level=risk_level,
            positions={s: Position(s, p.qty, p.available_qty, p.avg_cost)
                       for s, p in self.positions.items()},
            industry=dict(industry or {}),
            industry_caps=dict(industry_caps or {}),
            daily_pnl=float(daily_pnl),
            daily_loss_limit=float(daily_loss_limit),
        )

    # ---------- 成交约束 ----------

    def can_fill(self, order: OrderRequest, market_price: float, *,
                 limit_up: float | None = None,
                 limit_down: float | None = None,
                 suspended: bool = False) -> tuple[bool, str]:
        if suspended:
            return False, f"停牌不可成交: {order.symbol}"
        if market_price is None or market_price <= 0:
            return False, "行情价格非法"
        if order.side == "buy" and limit_up and market_price >= limit_up - PRICE_EPS:
            return False, f"涨停不可买: {order.symbol} 现价 {market_price:.2f}"
        if order.side == "sell" and limit_down and market_price <= limit_down + PRICE_EPS:
            return False, f"跌停不可卖: {order.symbol} 现价 {market_price:.2f}"
        if order.order_type == "limit" and order.limit_price is not None:
            if order.side == "buy" and market_price > order.limit_price + PRICE_EPS:
                return False, f"限价未穿越: {market_price:.2f} > {order.limit_price:.2f}"
            if order.side == "sell" and market_price < order.limit_price - PRICE_EPS:
                return False, f"限价未穿越: {market_price:.2f} < {order.limit_price:.2f}"
        if order.side == "sell":
            available = self.positions.get(order.symbol, Position(order.symbol)).available_qty
            if available < order.qty:
                return False, f"T+1 可用持仓不足: 可卖 {available} < {order.qty}"
        else:
            cost = market_price * order.qty + estimate_fees(
                "buy", market_price, order.qty, commission_rate=self.commission_rate,
                min_commission=self.min_commission, stamp_duty=self.stamp_duty)
            if cost > self.cash + 1e-6:
                return False, f"现金不足: 需 {cost:.2f} > 可用 {self.cash:.2f}"
        return True, ""

    # ---------- 撮合 ----------

    def fill(self, order: OrderRequest, market_price: float, *,
             limit_up: float | None = None,
             limit_down: float | None = None,
             suspended: bool = False) -> dict:
        ok, reason = self.can_fill(order, market_price, limit_up=limit_up,
                                   limit_down=limit_down, suspended=suspended)
        if not ok:
            logger.info("仿真拒单 %s: %s", order.id, reason)
            return {"ok": False, "reason": reason}

        direction = 1 if order.side == "buy" else -1
        exec_price = round(float(market_price) * (1 + direction * self.slippage), 4)
        turnover = exec_price * order.qty
        commission = round(max(turnover * self.commission_rate, self.min_commission), 2)
        stamp = round(turnover * self.stamp_duty, 2) if order.side == "sell" else 0.0
        fees = round(commission + stamp, 2)

        pos = self.positions.setdefault(order.symbol, Position(order.symbol))
        if order.side == "buy":
            total_cost = pos.avg_cost * pos.qty + turnover
            pos.qty += order.qty
            pos.avg_cost = round(total_cost / pos.qty, 4) if pos.qty else 0.0
            self._pending_buys[order.symbol] = self._pending_buys.get(order.symbol, 0) + order.qty
            self.cash -= turnover + fees
        else:
            pos.qty -= order.qty
            pos.available_qty -= order.qty
            self.realized_pnl += (exec_price - pos.avg_cost) * order.qty
            if pos.qty == 0:
                pos.avg_cost = 0.0
            self.cash += turnover - fees

        self.cash = round(self.cash, 2)
        self.total_fees = round(self.total_fees + fees, 2)
        self.realized_pnl = round(self.realized_pnl, 2)
        record = Fill(order.id, order.symbol, order.side, order.qty, exec_price,
                      commission, stamp, _now())
        self.fills.append(record)
        return {"ok": True, "fill": record.to_dict(), "cash": self.cash,
                "position": pos.to_dict()}

    # ---------- T+1 与对账 ----------

    def roll_day(self) -> None:
        """交易日切换：昨日买入解冻为可卖。"""
        for symbol, qty in self._pending_buys.items():
            pos = self.positions.setdefault(symbol, Position(symbol))
            pos.available_qty += qty
        self._pending_buys.clear()

    def reconcile(self, prices: Mapping[str, float]) -> dict:
        """每日对账：现金 + 市值 = 期初 + 已实现 + 浮动 − 费用。"""
        mv = self.market_value(prices)
        equity = round(self.cash + mv, 2)
        unrealized = round(sum(
            (float(prices.get(s, p.avg_cost)) - p.avg_cost) * p.qty
            for s, p in self.positions.items() if p.qty > 0), 2)
        expected = round(self.initial_cash + self.realized_pnl + unrealized
                         - self.total_fees, 2)
        diff = round(equity - expected, 4)
        return {
            "ok": abs(diff) <= RECONCILE_TOLERANCE,
            "cash": round(self.cash, 2),
            "market_value": mv,
            "equity": equity,
            "expected": expected,
            "diff": diff,
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": unrealized,
            "total_fees": round(self.total_fees, 2),
            "fills": len(self.fills),
        }
