"""G3 盘中闸（M10/P2）：tick 级市场状态检查 → 告警/禁买/禁卖/组合熔断。

与 G1/G2 的分工：G1/G2 是下单前的确定性账户规则；G3 是盘中持续监控，
不替代下单闸，而是把「涨停/跌停/停牌/流动性/组合回撤」变成可落库的告警。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.quant.backtest import limit_pct_for

#: 价格触及涨跌停的容差（比例）
LIMIT_EPS = 0.001


@dataclass(frozen=True)
class Tick:
    symbol: str
    last: float
    prev_close: float
    amount: float = 0.0
    volume: int = 0
    suspended: bool = False
    limit_pct: float | None = None   # ST 等特殊档位；缺省按代码段推断


@dataclass
class RiskAction:
    level: str        # warn | block | halt
    rule: str
    detail: str
    symbol: str = ""

    def to_dict(self) -> dict:
        return {"level": self.level, "rule": self.rule,
                "detail": self.detail, "symbol": self.symbol}


class IntradayGate:
    """tick 级检查：返回动作列表（空 = 正常）。"""

    name = "G3_market_state"

    def __init__(self, *, min_amount: float = 1_000_000.0,
                 turbulence_limit: float = 0.15):
        if not math.isfinite(min_amount) or min_amount < 0:
            raise ValueError("min_amount 必须为非负有限数")
        if not 0 < turbulence_limit <= 1:
            raise ValueError("turbulence_limit 必须在 (0, 1] 内")
        self.min_amount = float(min_amount)
        self.turbulence_limit = float(turbulence_limit)

    def on_tick(self, tick: Tick, *, portfolio_return: float = 0.0) -> list[RiskAction]:
        actions: list[RiskAction] = []
        if tick.suspended:
            actions.append(RiskAction("block", "suspended",
                                      f"{tick.symbol} 停牌，禁止成交", tick.symbol))
            return actions  # 停牌时价格无意义，不再判断涨跌停/流动性

        if tick.last <= 0 or tick.prev_close <= 0:
            actions.append(RiskAction("warn", "invalid_price",
                                      f"{tick.symbol} 行情价格非法 "
                                      f"(last={tick.last}, prev={tick.prev_close})",
                                      tick.symbol))
            return actions

        pct = (tick.limit_pct if tick.limit_pct is not None
               else limit_pct_for(tick.symbol)) / 100.0
        up = tick.prev_close * (1 + pct) * (1 - LIMIT_EPS)
        down = tick.prev_close * (1 - pct) * (1 + LIMIT_EPS)
        if tick.last >= up:
            actions.append(RiskAction("block", "limit_up",
                                      f"{tick.symbol} 涨停 {tick.last:.2f}，禁止买入",
                                      tick.symbol))
        elif tick.last <= down:
            actions.append(RiskAction("block", "limit_down",
                                      f"{tick.symbol} 跌停 {tick.last:.2f}，禁止卖出",
                                      tick.symbol))

        if tick.amount < self.min_amount:
            actions.append(RiskAction(
                "warn", "low_liquidity",
                f"{tick.symbol} 成交额 {tick.amount / 10_000:.0f} 万低于门槛",
                tick.symbol))

        if portfolio_return <= -self.turbulence_limit:
            actions.append(RiskAction(
                "halt", "portfolio_circuit_breaker",
                f"组合回撤 {portfolio_return:.1%} 触发熔断（阈值 "
                f"{self.turbulence_limit:.0%}）"))
        return actions

    def blocked_symbols(self, actions: list[RiskAction]) -> dict[str, set[str]]:
        """把动作折叠成 {buy/sell: 禁买/禁卖标的集合}，供撮合前快速判断。"""
        out: dict[str, set[str]] = {"buy": set(), "sell": set()}
        for action in actions:
            if action.rule == "limit_up" and action.symbol:
                out["buy"].add(action.symbol)
            elif action.rule == "limit_down" and action.symbol:
                out["sell"].add(action.symbol)
            elif action.rule == "suspended" and action.symbol:
                out["buy"].add(action.symbol)
                out["sell"].add(action.symbol)
        return out
