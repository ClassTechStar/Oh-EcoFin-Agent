"""订单与风控事件落库（报告 5.9 orders / risk_events）：100% 可回溯。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from backend.db import Database
from backend.execution.models import OrderRequest, RiskDecision

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class ExecutionStore:
    """同步读写封装；调用方负责线程池（与其它 Store 同约定）。"""

    def __init__(self, db: Database):
        self.db = db

    def save_order(self, order: OrderRequest, status: str,
                   decision: RiskDecision | None = None,
                   broker_order_id: str = "") -> None:
        now = _now()
        self.db.execute(
            "INSERT OR REPLACE INTO orders"
            "(id, strategy_id, symbol, side, qty, price, order_type, status,"
            " risk_decision_json, broker_order_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (order.id, order.strategy_id, order.symbol, order.side, order.qty,
             order.price, order.order_type, status,
             json.dumps(decision.to_dict() if decision else {}, ensure_ascii=False),
             broker_order_id, now, now),
        )

    def update_status(self, order_id: str, status: str,
                      broker_order_id: str = "") -> None:
        self.db.execute(
            "UPDATE orders SET status=?, broker_order_id=?, updated_at=? WHERE id=?",
            (status, broker_order_id, _now(), order_id),
        )

    def log_risk_event(self, decision: RiskDecision, *, order_id: str,
                       symbol: str, rule: str = "pre_trade") -> int:
        """每次闸门评估都落库（pass/warn/block 全量，便于事后复核）。"""
        return self.db.execute(
            "INSERT INTO risk_events(level, gate, rule, detail, symbol, order_id, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (decision.severity, decision.gate, rule,
             "；".join(decision.reasons) or "通过", symbol, order_id, _now()),
        )

    def recent_orders(self, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM orders ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(limit),))
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["risk_decision"] = json.loads(item.pop("risk_decision_json") or "{}")
            except json.JSONDecodeError:
                logger.warning("订单 %s 的风控快照解析失败", item.get("id"))
                item["risk_decision"] = {}
            out.append(item)
        return out

    def recent_risk_events(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (int(limit),))
