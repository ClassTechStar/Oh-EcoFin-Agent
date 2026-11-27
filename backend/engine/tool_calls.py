"""工具调用原始结果存储（M6 记忆/审计）。

进入模型上下文的是压缩结果；这里保存**原始** args/result，供 M7 证据账本、
查询审计与失败复盘使用。
"""

from __future__ import annotations

import json

from backend.db import Database


class ToolCallStore:
    def __init__(self, db: Database):
        self.db = db

    def record(self, session_id: str, name: str, raw_args: str, result: dict,
               ms: int, ok: bool, cached: bool = False) -> int:
        try:
            args = json.loads(raw_args or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {"raw": raw_args}
        return self.db.insert_tool_call(
            session_id, name, args, result, ms, ok, cached)

    def recent(self, session_id: str = "", limit: int = 50) -> list[dict]:
        if session_id:
            return self.db.query(
                "SELECT * FROM tool_calls WHERE session_id=?"
                " ORDER BY id DESC LIMIT ?", (session_id, limit))
        return self.db.query(
            "SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (limit,))
