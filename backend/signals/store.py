"""信号与目标权重的落库（报告 5.9 alpha_signals / portfolio_targets）。"""

from __future__ import annotations

import json
import logging

from backend.db import Database
from backend.signals.base import AlphaSignal

logger = logging.getLogger(__name__)


class SignalStore:
    """同步读写封装；调用方负责线程池（与 AuditStore 同约定）。"""

    def __init__(self, db: Database):
        self.db = db

    def save_signals(self, signals: list[AlphaSignal]) -> int:
        """逐条落库（含弃权票：弃权本身是审计信息）。"""
        count = 0
        with self.db.tx() as conn:
            for sig in signals:
                conn.execute(
                    "INSERT INTO alpha_signals"
                    "(model_name, symbol, as_of, value, confidence, horizon_days,"
                    " abstained, evidence_json) VALUES(?,?,?,?,?,?,?,?)",
                    (sig.model_name, sig.symbol, sig.as_of, float(sig.value),
                     float(sig.confidence), int(sig.horizon_days), int(sig.abstained),
                     json.dumps([e.model_dump() for e in sig.evidence],
                                ensure_ascii=False)),
                )
                count += 1
        return count

    def save_target(self, strategy_id: str, as_of: str, weights: dict[str, float],
                    constraints: list[dict] | None = None,
                    flat_reason: str | None = None) -> int:
        """保存一次目标权重；flat_reason 非空表示本轮不建仓（显式记录原因）。"""
        return self.db.execute(
            "INSERT INTO portfolio_targets"
            "(strategy_id, as_of, weights_json, constraints_json, flat_reason)"
            " VALUES(?,?,?,?,?)",
            (strategy_id, as_of,
             json.dumps(weights, ensure_ascii=False, sort_keys=True),
             json.dumps(constraints or [], ensure_ascii=False, default=str),
             flat_reason),
        )

    def recent_signals(self, *, model_name: str | None = None,
                       symbol: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM alpha_signals WHERE 1=1"
        params: list = []
        if model_name:
            sql += " AND model_name=?"
            params.append(model_name)
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, tuple(params))
        out = []
        for row in rows:
            item = dict(row)
            item["abstained"] = bool(item["abstained"])
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
            except json.JSONDecodeError:
                logger.warning("信号 %s 的 evidence_json 解析失败", item.get("id"))
                item["evidence"] = []
            out.append(item)
        return out

    def recent_targets(self, *, strategy_id: str | None = None,
                       limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM portfolio_targets"
        params: list = []
        if strategy_id:
            sql += " WHERE strategy_id=?"
            params.append(strategy_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, tuple(params))
        out = []
        for row in rows:
            item = dict(row)
            for key, target in (("weights_json", "weights"),
                                ("constraints_json", "constraints")):
                raw = item.pop(key, None)
                try:
                    item[target] = json.loads(raw) if raw else ({} if target == "weights" else [])
                except json.JSONDecodeError:
                    logger.warning("目标权重 %s 的 %s 解析失败", item.get("id"), key)
                    item[target] = {}
            out.append(item)
        return out
