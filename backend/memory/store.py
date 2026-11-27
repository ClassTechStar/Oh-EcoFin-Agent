"""会话与消息持久化（SQLite 落库；会话切换 = SSE 重放见 API 层）。"""

from __future__ import annotations

import uuid

from backend.db import Database


def _now() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class SessionStore:
    def __init__(self, db: Database):
        self.db = db

    def create(self, title: str = "") -> dict:
        sid = uuid.uuid4().hex[:16]
        self.db.execute(
            "INSERT INTO sessions(id, title, created_at, status) VALUES(?,?,?,?)",
            (sid, title, _now(), "active"),
        )
        return {"id": sid, "title": title}

    def list_sessions(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT id, title, created_at, status FROM sessions"
            " ORDER BY created_at DESC LIMIT ?", (limit,))

    def get(self, session_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def rename(self, session_id: str, title: str) -> None:
        self.db.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))

    def close(self, session_id: str) -> None:
        self.db.execute("UPDATE sessions SET status='closed' WHERE id=?", (session_id,))

    def messages(self, session_id: str, limit: int = 500) -> list[dict]:
        return self.db.query(
            "SELECT role, content, tool_call_id, tokens, created_at FROM messages"
            " WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (session_id, limit))

    def last_user_message(self, session_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT content FROM messages WHERE session_id=? AND role='user'"
            " ORDER BY id DESC LIMIT 1", (session_id,))
        return row["content"] if row else None

    def insert_message(self, session_id: str, role: str, content: str,
                       tool_call_id: str | None = None, tokens: int = 0) -> int:
        return self.db.insert_message(session_id, role, content,
                                      tool_call_id=tool_call_id, tokens=tokens)
