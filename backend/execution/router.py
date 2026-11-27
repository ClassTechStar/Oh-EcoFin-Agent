"""OrderRouter：闸链评估 → 仿真撮合 → 订单/风控事件落库（M10 前两闸闭环）。"""

from __future__ import annotations

import logging

from backend.execution.gates import GateChain
from backend.execution.models import AccountContext, OrderRequest, RiskDecision
from backend.execution.paper import PaperBroker
from backend.execution.store import ExecutionStore

logger = logging.getLogger(__name__)


class OrderRouter:
    """同步路由（调用方进线程池）；风控在撮合前，不可被调用方跳过。"""

    def __init__(self, gates: GateChain, broker: PaperBroker,
                 store: ExecutionStore | None = None):
        self.gates = gates
        self.broker = broker
        self.store = store

    def submit(self, order: OrderRequest, ctx: AccountContext, *,
               market_price: float,
               limit_up: float | None = None,
               limit_down: float | None = None,
               suspended: bool = False) -> dict:
        decision = self.gates.evaluate(order, ctx)
        if self.store is not None:
            self.store.save_order(order, "created", decision)
            # 逐闸留痕：G1/G2 每次评估都落库（pass 也记录，便于复核）
            for item in (decision.trace or [decision.to_dict()]):
                self.store.log_risk_event(
                    RiskDecision(item["allowed"], item["adjusted_qty"],
                                 item["reasons"], item["severity"], item["gate"]),
                    order_id=order.id, symbol=order.symbol)

        if not decision.allowed:
            if self.store is not None:
                self.store.update_status(order.id, "rejected")
            return {"ok": False, "order_id": order.id,
                    "status": "rejected", "decision": decision.to_dict(),
                    "reason": "；".join(decision.reasons) or "风控拒绝"}

        adjusted = order.model_copy(update={"qty": decision.adjusted_qty})
        result = self.broker.fill(adjusted, market_price, limit_up=limit_up,
                                 limit_down=limit_down, suspended=suspended)
        if not result.get("ok"):
            broker_block = RiskDecision(False, 0, [result.get("reason", "撮合失败")],
                                        "block", "broker")
            if self.store is not None:
                self.store.update_status(order.id, "rejected")
                self.store.log_risk_event(broker_block, order_id=order.id,
                                          symbol=order.symbol, rule="match")
            return {"ok": False, "order_id": order.id, "status": "rejected",
                    "decision": decision.to_dict(), "reason": result.get("reason", "")}

        broker_order_id = f"paper-{order.id}"
        if self.store is not None:
            self.store.update_status(order.id, "filled", broker_order_id)
        return {"ok": True, "order_id": order.id, "status": "filled",
                "decision": decision.to_dict(), "fill": result["fill"],
                "cash": result["cash"], "position": result["position"]}
