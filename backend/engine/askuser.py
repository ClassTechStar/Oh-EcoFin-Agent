"""ask_user 挂起机制：多轮交互的通用基础设施（不是投顾专用补丁）。

流程：
① 引擎收到挂起类工具调用（ask_risk_question）→ 不执行、不回填 tool result，
   create() 置 PENDING_ASK（含 qid）→ 事件流发 ask_user{qid, question, options} →
   wait() 请求协程 await Future 挂起（不占线程；超时 10 分钟自动取消并提示）；
② 用户点选 → POST /api/chat/resume {session, qid, choice} → resume() 注入 Future 续跑；
③ 挂起状态持久化 SQLite（进程崩溃/断线重连后可恢复）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from backend.db import Database

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PendingAsk:
    session_id: str
    qid: str
    question: str
    options: list[str]
    context: dict = field(default_factory=dict)
    future: asyncio.Future | None = None


class AskUserManager:
    def __init__(self, db: Database, timeout_seconds: int = 600):
        self.db = db
        self.timeout = timeout_seconds
        self._pending: dict[str, PendingAsk] = {}  # session_id → PendingAsk

    async def create(self, session_id: str, question: str, options: list[str],
                     context: dict | None = None) -> PendingAsk:
        """创建挂起并持久化。"""
        pa = PendingAsk(
            session_id=session_id, qid=uuid.uuid4().hex[:10],
            question=question, options=options, context=context or {})
        self._pending[session_id] = pa
        await self._run_db(self._persist, session_id, pa.qid, question, options)
        return pa

    async def wait(self, pa: PendingAsk) -> dict:
        """等待用户作答（协程挂起不占线程；超时自动取消）。"""
        pa.future = asyncio.get_running_loop().create_future()
        try:
            choice = await asyncio.wait_for(pa.future, timeout=self.timeout)
            await self._run_db(self._mark, pa.session_id, "answered")
            return {"qid": pa.qid, "choice": choice, "timeout": False}
        except TimeoutError:
            await self._run_db(self._mark, pa.session_id, "timeout")
            return {"qid": pa.qid, "choice": None, "timeout": True}
        finally:
            self._pending.pop(pa.session_id, None)

    async def ask(self, session_id: str, question: str, options: list[str],
                  context: dict | None = None) -> dict:
        """便捷组合：create + wait。"""
        pa = await self.create(session_id, question, options, context)
        return await self.wait(pa)

    async def resume(self, session_id: str, qid: str, choice: str) -> bool:
        """用户点选 → 注入挂起中的 Future。"""
        pa = self._pending.get(session_id)
        if pa is None or pa.qid != qid:
            return False
        if pa.future is not None and not pa.future.done():
            pa.future.set_result(choice)
        return True

    def is_pending(self, session_id: str) -> bool:
        return session_id in self._pending

    def pending_info(self, session_id: str) -> dict | None:
        pa = self._pending.get(session_id)
        if pa is None:
            return None
        return {"qid": pa.qid, "question": pa.question, "options": pa.options,
                "context": pa.context}

    # ---------- SQLite 持久化 ----------

    async def _run_db(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def _persist(self, session_id, qid, question, options):
        self.db.execute(
            "INSERT OR REPLACE INTO pending_asks(session_id, qid, question,"
            " options_json, state, created_at) VALUES(?,?,?,?,?,?)",
            (session_id, qid, question, str(options), "pending", _now()))

    def _mark(self, session_id, state):
        self.db.execute(
            "UPDATE pending_asks SET state=? WHERE session_id=?", (state, session_id))
