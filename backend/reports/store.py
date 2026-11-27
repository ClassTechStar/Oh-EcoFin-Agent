"""报告元数据存储（M5）：reports + report_files。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend.db import Database


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class ReportStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, report_id: str, title: str, subtitle: str, status: str,
             spec: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO reports(id, title, subtitle, status, spec_json,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (report_id, title, subtitle, status,
             json.dumps(spec, ensure_ascii=False, default=str),
             spec.get("created_at") or _now()))

    def add_file(self, report_id: str, fmt: str, filename: str, path: str,
                 sha256: str, size: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO report_files(id, report_id, format, filename,"
            " path, sha256, bytes, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"{report_id}:{fmt}", report_id, fmt, filename, path, sha256,
             size, _now()))

    def get(self, report_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM reports WHERE id=?", (report_id,))
        if not row:
            return None
        try:
            spec = json.loads(row.get("spec_json") or "{}")
        except json.JSONDecodeError:
            spec = {}
        return {
            "report_id": row["id"],
            "title": row["title"],
            "subtitle": row["subtitle"] or "",
            "status": row["status"],
            "created_at": row["created_at"],
            "spec": spec,
            "files": self.files(report_id),
        }

    def files(self, report_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM report_files WHERE report_id=? ORDER BY format",
            (report_id,))
        return [{
            "format": r["format"],
            "filename": r["filename"],
            "path": r["path"],
            "sha256": r["sha256"],
            "bytes": int(r["bytes"] or 0),
        } for r in rows]

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT id, title, subtitle, status, spec_json, created_at FROM reports"
            " ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            try:
                spec = json.loads(row.get("spec_json") or "{}")
            except json.JSONDecodeError:
                spec = {}
            audit = spec.get("audit") or {}
            out.append({
                "report_id": row["id"],
                "title": row["title"],
                "subtitle": row["subtitle"] or "",
                "status": row["status"],
                "created_at": row["created_at"],
                "block_count": len(spec.get("blocks") or []),
                "dataset_count": len(spec.get("datasets") or []),
                "spec_hash": spec.get("spec_hash", ""),
                "audit_status": audit.get("status", ""),
                "evidence_count": audit.get("evidence_count", 0),
                "claims_total": audit.get("claims_total", 0),
                "claims_failed": audit.get("claims_failed", 0),
                "files": self.files(row["id"]),
            })
        return out

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM reports")
        return int(row["c"]) if row else 0
