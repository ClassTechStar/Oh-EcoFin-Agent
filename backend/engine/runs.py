"""Agent run 记录（M6 记忆/审计基础）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.db import Database
from backend.engine.run_state import AgentRun


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class RunStore:
    def __init__(self, db: Database):
        self.db = db

    def start(self, run: AgentRun) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO agent_runs(id, session_id, mode, state,"
            " stop_reason, rounds, tool_calls, tool_errors, tool_blocked,"
            " tokens_in, tokens_out, cost_cents, summary, started_at, ended_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.session_id, run.mode, run.state.value, "",
             0, 0, 0, 0, 0, 0, 0, "", run.started_at, ""))

    def finish(self, run: AgentRun, *, summary: str = "") -> None:
        self.db.execute(
            "UPDATE agent_runs SET state=?, stop_reason=?, rounds=?, tool_calls=?,"
            " tool_errors=?, tool_blocked=?, tokens_in=?, tokens_out=?,"
            " cost_cents=?, summary=?, ended_at=? WHERE id=?",
            (run.state.value, run.stop_reason, run.rounds, run.tool_calls,
             run.tool_errors, run.tool_blocked, run.tokens_in, run.tokens_out,
             run.cost_cents, (summary or run.summary)[:1000],
             run.ended_at or _now(), run.run_id))

    def get(self, run_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    def recent(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT * FROM agent_runs ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,))

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM agent_runs")
        return int(row["c"]) if row else 0
