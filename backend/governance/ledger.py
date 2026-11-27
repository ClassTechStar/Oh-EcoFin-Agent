"""治理账本（P3）：append-only 哈希链，任何改写都会被 `verify()` 定位。

设计：
- `payload_hash = sha256(prev_hash + kind + ref_id + 规范化 payload)`；
- 首条 `prev_hash = GENESIS`，后续必须等于上一条的 `payload_hash`；
- 落库同时保留 `payload_json`：否则只能验证「链是否断」，无法验证「内容是否被换」；
- 全表只读语义：本模块不提供 update/delete。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from backend.db import Database

logger = logging.getLogger(__name__)

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _hash(prev_hash: str, kind: str, ref_id: str, payload: object) -> str:
    raw = f"{prev_hash}|{kind}|{ref_id}|{_canonical(payload)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Ledger:
    """哈希链账本（同步；调用方负责线程池）。"""

    def __init__(self, db: Database):
        self.db = db

    def append(self, kind: str, ref_id: str, payload: dict) -> dict:
        if not kind or not ref_id:
            raise ValueError("kind / ref_id 不能为空")
        last = self.last()
        prev_hash = last["payload_hash"] if last else GENESIS
        payload_hash = _hash(prev_hash, kind, ref_id, payload)
        seq = self.db.execute(
            "INSERT INTO ledger(kind, ref_id, payload_json, payload_hash, prev_hash,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (kind, ref_id, _canonical(payload), payload_hash, prev_hash, _now()),
        )
        return {"seq": seq, "kind": kind, "ref_id": ref_id,
                "payload_hash": payload_hash, "prev_hash": prev_hash}

    def last(self) -> dict | None:
        return self.db.query_one("SELECT * FROM ledger ORDER BY seq DESC LIMIT 1")

    def entries(self, limit: int = 100) -> list[dict]:
        return self.db.query("SELECT * FROM ledger ORDER BY seq ASC LIMIT ?",
                             (int(limit),))

    def verify(self) -> dict:
        """逐条重算哈希并校验前向链接；返回首个断点。"""
        rows = self.db.query("SELECT * FROM ledger ORDER BY seq ASC")
        prev_hash = GENESIS
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                return {"ok": False, "broken_at": row["seq"],
                        "reason": "payload_json 解析失败", "count": len(rows)}
            if row["prev_hash"] != prev_hash:
                return {"ok": False, "broken_at": row["seq"],
                        "reason": "prev_hash 与上一条不衔接", "count": len(rows)}
            expect = _hash(prev_hash, row["kind"], row["ref_id"], payload)
            if expect != row["payload_hash"]:
                return {"ok": False, "broken_at": row["seq"],
                        "reason": "payload_hash 与内容不匹配（疑似篡改）",
                        "count": len(rows)}
            prev_hash = row["payload_hash"]
        return {"ok": True, "broken_at": None, "reason": "", "count": len(rows)}
