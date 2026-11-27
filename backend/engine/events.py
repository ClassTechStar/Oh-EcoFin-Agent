"""SSE v2 事件协议：会话级单调递增 id + 环形缓冲重放。

事件类型：
  message_delta / thinking_delta / tool_call / chart /
  ask_user / run_state / budget / done / error / ping

M6 约定：`done` 是唯一终态（一次 run 恰一个）；`error`/`budget` 是过程事件。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SSEEvent:
    id: int
    event: str
    payload: dict[str, Any]

    def sse_format(self) -> str:
        """text/event-stream 单条帧。"""
        import json

        data = json.dumps(self.payload, ensure_ascii=False, default=str)
        return f"id: {self.id}\nevent: {self.event}\ndata: {data}\n\n"


@dataclass
class SessionStream:
    """单会话事件流：单调 id + 内存环形缓冲 + SQLite 兜底重放。"""

    session_id: str
    buffer_size: int = 500
    _next_id: int = 1
    _buffer: deque[SSEEvent] = field(default_factory=deque)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    persist_fn: Any = None  # callable(event, payload) → 异步落库（断线重连兜底）

    def next_id(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid

    def publish(self, event: str, payload: dict[str, Any]) -> SSEEvent:
        ev = SSEEvent(id=self.next_id(), event=event, payload=payload)
        self._buffer.append(ev)
        while len(self._buffer) > self.buffer_size:
            self._buffer.popleft()
        for q in list(self._subscribers):
            q.put_nowait(ev)
        if self.persist_fn is not None:
            try:
                import asyncio

                asyncio.get_running_loop().create_task(
                    asyncio.to_thread(self.persist_fn, ev.id, event, payload))
            except RuntimeError:
                pass
        return ev

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def replay_from(self, last_event_id: int, db_rows: list[dict]) -> list[SSEEvent]:
        """断线重连重放：先取 SQLite 兜底（环形缓冲可能已溢出），再合并内存缓冲。

        db_rows: [{id, event, payload}] —— last_event_id 之后落库的全部事件。
        """
        merged: dict[int, SSEEvent] = {}
        for r in db_rows:
            merged[r["id"]] = SSEEvent(id=r["id"], event=r["event"], payload=r["payload"])
        for ev in self._buffer:
            if ev.id > last_event_id:
                merged[ev.id] = ev
        return [merged[k] for k in sorted(merged)]


class StreamHub:
    """全部会话流的注册中心。"""

    def __init__(self, buffer_size: int = 500):
        self.buffer_size = buffer_size
        self._streams: dict[str, SessionStream] = {}

    def get(self, session_id: str) -> SessionStream:
        if session_id not in self._streams:
            self._streams[session_id] = SessionStream(session_id, self.buffer_size)
        return self._streams[session_id]


def make_event(event: str, **payload) -> tuple[str, dict]:
    return event, payload


def ping_event() -> SSEEvent:
    return SSEEvent(id=0, event="ping", payload={})
