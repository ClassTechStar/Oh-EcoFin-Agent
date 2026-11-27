"""证据/数字声明/审计事件存储（M7）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend.db import Database


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class AuditStore:
    def __init__(self, db: Database):
        self.db = db

    def replace(self, report_id: str, evidence: list[dict], claims: list[dict],
                events: list[dict]) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM evidence WHERE report_id=?", (report_id,))
            conn.execute("DELETE FROM number_claims WHERE report_id=?", (report_id,))
            conn.execute("DELETE FROM audit_events WHERE report_id=?", (report_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO evidence(id, report_id, run_id, tool_call_id,"
                " block_id, source, locator, excerpt, value_hash, value_num,"
                " value_text, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(item["id"], item["report_id"], item["run_id"], item["tool_call_id"],
                  item["block_id"], item["source"], item["locator"], item["excerpt"],
                  item["value_hash"], item.get("value_num"), item.get("value_text"),
                  _now()) for item in evidence])
            conn.executemany(
                "INSERT OR REPLACE INTO number_claims(id, report_id, block_id, claim,"
                " value, unit, evidence_ids_json, passed, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [(item["id"], item["report_id"], item["block_id"], item["claim"],
                  item["value"], item["unit"],
                  json.dumps(item["evidence_ids"], ensure_ascii=False),
                  int(item["passed"]), _now()) for item in claims])
            conn.executemany(
                "INSERT INTO audit_events(report_id, level, gate, detail, created_at)"
                " VALUES(?,?,?,?,?)",
                [(report_id, event.get("level", "info"), event.get("gate", ""),
                  event.get("detail", ""), _now()) for event in events])

    def get(self, report_id: str) -> dict:
        evidence = self.db.query(
            "SELECT * FROM evidence WHERE report_id=? ORDER BY block_id, locator",
            (report_id,))
        claims = self.db.query(
            "SELECT * FROM number_claims WHERE report_id=?"
            " ORDER BY passed ASC, block_id, id", (report_id,))
        events = self.db.query(
            "SELECT * FROM audit_events WHERE report_id=? ORDER BY id", (report_id,))
        for claim in claims:
            try:
                claim["evidence_ids"] = json.loads(
                    claim.get("evidence_ids_json") or "[]")
            except json.JSONDecodeError:
                claim["evidence_ids"] = []
            claim["passed"] = bool(claim.get("passed"))
        return {"evidence": evidence, "claims": claims, "events": events}

    def summary(self, report_id: str) -> dict:
        data = self.get(report_id)
        return {
            "evidence_count": len(data["evidence"]),
            "claims_total": len(data["claims"]),
            "claims_failed": sum(1 for c in data["claims"] if not c["passed"]),
        }
