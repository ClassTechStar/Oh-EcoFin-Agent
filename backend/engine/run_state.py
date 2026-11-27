"""Agent 运行状态机与预算（M6）。

一次 run 的生命周期：
planning → acting → observing → planning … → synthesizing → done
异常/预算耗尽 → failed
ask_user 挂起期间为 waiting_user。

终态只允许 done / failed；引擎通过 `terminal_emitted` 保证一次 run 恰一个
done 事件（error 事件可以出现多次，但不是终态）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class RunState(str, Enum):
    PLANNING = "planning"
    ACTING = "acting"
    OBSERVING = "observing"
    WAITING_USER = "waiting_user"
    SYNTHESIZING = "synthesizing"
    DONE = "done"
    FAILED = "failed"


TERMINAL_STATES = {RunState.DONE, RunState.FAILED}

_ALLOWED: dict[RunState, set[RunState]] = {
    RunState.PLANNING: {RunState.ACTING, RunState.WAITING_USER,
                        RunState.SYNTHESIZING, RunState.FAILED},
    RunState.ACTING: {RunState.OBSERVING, RunState.WAITING_USER, RunState.FAILED},
    RunState.OBSERVING: {RunState.PLANNING, RunState.SYNTHESIZING, RunState.FAILED},
    RunState.WAITING_USER: {RunState.PLANNING, RunState.FAILED},
    RunState.SYNTHESIZING: {RunState.DONE, RunState.FAILED},
    RunState.DONE: set(),
    RunState.FAILED: set(),
}


@dataclass
class AgentRun:
    run_id: str
    session_id: str
    mode: str
    state: RunState = RunState.PLANNING
    started_at: str = field(default_factory=_now)
    ended_at: str = ""
    rounds: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_blocked: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cents: int = 0
    stop_reason: str = ""
    error: str = ""
    summary: str = ""
    transitions: list[tuple[str, str]] = field(default_factory=list)
    terminal_emitted: bool = False
    done_payload: dict | None = None

    @classmethod
    def create(cls, session_id: str, mode: str) -> AgentRun:
        return cls(run_id=f"run_{uuid.uuid4().hex[:12]}",
                   session_id=session_id, mode=mode)

    def transition(self, next_state: RunState) -> None:
        if next_state not in _ALLOWED[self.state]:
            raise ValueError(
                f"非法状态迁移: {self.state.value} -> {next_state.value}")
        previous = self.state
        self.state = next_state
        self.transitions.append((previous.value, next_state.value))
        if next_state in TERMINAL_STATES:
            self.ended_at = _now()

    def can(self, next_state: RunState) -> bool:
        return next_state in _ALLOWED[self.state]

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "mode": self.mode,
            "state": self.state.value,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "tool_blocked": self.tool_blocked,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_cents": self.cost_cents,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "transitions": [list(t) for t in self.transitions],
        }


@dataclass(frozen=True)
class RunBudget:
    max_rounds: int = 12
    context_tokens: int = 32000
    keep_recent_messages: int = 12
    tool_result_tokens: int = 2000
    total_tool_tokens: int = 12000

    @classmethod
    def from_config(cls, cfg, *, max_rounds: int = 12) -> RunBudget:
        return cls(
            max_rounds=int(max_rounds),
            context_tokens=int(cfg.context_token_budget),
            keep_recent_messages=int(cfg.keep_recent_messages),
            tool_result_tokens=int(cfg.tool_result_token_budget),
            total_tool_tokens=int(cfg.total_tool_token_budget),
        )
