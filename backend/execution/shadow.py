"""影子账户（P2/S3）：真实行情 + 虚拟资金 + 完整风控，逐日对账与穿透审计。

用途：实盘前验证风控闸与告警链路。影子账户的所有委托都必须经过 `OrderRouter`，
不存在"直接调 broker"的旁路；每日结束执行对账恒等式并留存权益曲线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from backend.execution.models import OrderRequest
from backend.execution.paper import PaperBroker
from backend.execution.router import OrderRouter

logger = logging.getLogger(__name__)


@dataclass
class ShadowDay:
    day: str
    equity: float
    cash: float
    reconcile_ok: bool
    fills: int
    blocked: int
    note: str = ""

    def to_dict(self) -> dict:
        return {"day": self.day, "equity": self.equity, "cash": self.cash,
                "reconcile_ok": self.reconcile_ok, "fills": self.fills,
                "blocked": self.blocked, "note": self.note}


class ShadowAccount:
    """跨日影子账户：start_day → submit* → end_day（自动 T+1 解冻）。"""

    def __init__(self, router: OrderRouter, broker: PaperBroker, *,
                 risk_level: str = "R3",
                 industry: Mapping[str, str] | None = None,
                 industry_caps: Mapping[str, float] | None = None,
                 daily_loss_limit: float = 0.02):
        self.router = router
        self.broker = broker
        self.risk_level = risk_level
        self.industry = dict(industry or {})
        self.industry_caps = dict(industry_caps or {})
        self.daily_loss_limit = daily_loss_limit
        self.days: list[ShadowDay] = []
        self.breaches: list[str] = []
        self._day = ""
        self._day_open_equity = 0.0
        self._blocked_today = 0

    # ---------- 日循环 ----------

    def start_day(self, day: str, prices: Mapping[str, float]) -> None:
        if self._day:
            raise RuntimeError(f"上一交易日 {self._day} 尚未 end_day")
        self._day = day
        self._day_open_equity = self.broker.context(prices).total_equity
        self._blocked_today = 0

    def _context(self, prices: Mapping[str, float]):
        equity = self.broker.context(prices).total_equity
        return self.broker.context(
            prices, risk_level=self.risk_level,
            industry=self.industry, industry_caps=self.industry_caps,
            daily_pnl=equity - self._day_open_equity,
            daily_loss_limit=self.daily_loss_limit)

    def submit(self, order: OrderRequest, prices: Mapping[str, float], *,
               limit_up: float | None = None, limit_down: float | None = None,
               suspended: bool = False) -> dict:
        if not self._day:
            raise RuntimeError("先调用 start_day")
        result = self.router.submit(
            order, self._context(prices),
            market_price=float(prices.get(order.symbol, order.price)),
            limit_up=limit_up, limit_down=limit_down, suspended=suspended)
        decision = result.get("decision") or {}
        if decision.get("severity") == "block":
            self._blocked_today += 1
        return result

    def end_day(self, prices: Mapping[str, float]) -> ShadowDay:
        if not self._day:
            raise RuntimeError("未 start_day")
        check = self.broker.reconcile(prices)
        if not check["ok"]:
            self.breaches.append(
                f"{self._day} 对账不平: diff={check['diff']}")
        entry = ShadowDay(day=self._day, equity=check["equity"],
                          cash=check["cash"], reconcile_ok=check["ok"],
                          fills=check["fills"], blocked=self._blocked_today)
        self.days.append(entry)
        self.broker.roll_day()          # T+1：当日买入次日可卖
        self._day = ""
        self._day_open_equity = 0.0
        return entry

    @property
    def equity_curve(self) -> list[dict]:
        return [{"day": d.day, "equity": d.equity} for d in self.days]

    def summary(self) -> dict:
        return {
            "days": len(self.days),
            "breaches": list(self.breaches),
            "equity_curve": self.equity_curve,
            "final_equity": self.days[-1].equity if self.days else None,
            "fills": self.broker.fills and len(self.broker.fills) or 0,
        }
