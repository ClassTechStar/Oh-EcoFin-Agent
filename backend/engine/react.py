"""ReAct 主循环（四重优化）：

① 并行工具调用（asyncio.gather，3 股对比端到端 ~4s）
② 结果压缩器（进上下文前降维，原始结果照常落库 tool_calls 表）
③ 参数沙箱（违例参数 100% 拒绝且回错误提示，自动重试 1 次上限）
④ 结构化输出（submit_research_report 以 Function Calling 强约束 JSON）
另有 ask_user 挂起拦截、轮次上限、成本聚合（done 事件）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend.engine.askuser import AskUserManager
from backend.engine.compactor import pack as compact_result
from backend.engine.events import SessionStream
from backend.engine.gateway import LLMGateway, RecoverableLLMError
from backend.engine.prompts import DISCLAIMER, SYSTEM_PROMPT
from backend.memory.summarize import estimate_tokens
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_ROUNDS = 12

# 挂起类工具：引擎不执行、不回填，转为 ask_user 事件挂起
ASK_TOOLS = {"ask_risk_question"}


@dataclass
class ChatTask:
    session_id: str
    user_message: str
    history: list[dict] = field(default_factory=list)   # [{role, content}]
    history_block: str = ""                             # 决策记忆注入块
    mode: str = "chat"                                  # chat | debate | advisor


class ReActEngine:
    def __init__(self, gateway: LLMGateway, registry: ToolRegistry,
                 ask_manager: AskUserManager):
        self.gw = gateway
        self.registry = registry
        self.ask_manager = ask_manager

    async def run(self, task: ChatTask, stream: SessionStream) -> dict:
        """执行一次完整会话轮；返回 done payload（同时发 done 事件）。"""
        messages = self._assemble(task)
        usage = {"tokens_in": 0, "tokens_out": 0, "cost_cents": 0, "calls": 0}
        tool_stats: dict[str, Any] = {"calls": 0, "errors": 0, "cached": 0, "ms_total": 0}
        charts_emitted: list[str] = []
        text_answer = ""
        self._last_report: dict | None = None

        for round_no in range(1, MAX_ROUNDS + 1):
            try:
                resp = await self.gw.chat(
                    messages, tools=self.registry.schemas(),
                    stream_cb=self._stream_cb(stream), session_id=task.session_id,
                    role=f"react:{task.mode}",
                )
            except RecoverableLLMError as e:
                self._emit(stream, "error", {
                    "code": "LLM_UNAVAILABLE", "message": str(e), "recoverable": True})
                return self._done(usage, tool_stats, rounds=round_no, error=str(e),
                                  charts=charts_emitted, stream=stream)
            except Exception as e:  # 成本熔断等不可恢复
                self._emit(stream, "error", {
                    "code": type(e).__name__, "message": str(e), "recoverable": False})
                return self._done(usage, tool_stats, rounds=round_no, error=str(e),
                                  charts=charts_emitted, stream=stream)

            usage["tokens_in"] += resp.usage_in
            usage["tokens_out"] += resp.usage_out
            usage["cost_cents"] += resp.cost_cents
            usage["calls"] += 1

            if resp.tool_calls and (ask := self._detect_ask(resp)):
                # ③ ask_user 挂起：不执行、不回填，事件挂起等待点选
                pa = await self.ask_manager.create(
                    task.session_id, ask["question"], ask.get("options", []),
                    ask.get("context"))
                self._emit(stream, "ask_user", {
                    "qid": pa.qid, "question": pa.question,
                    "options": pa.options, "context": pa.context})
                answer = await self.ask_manager.wait(pa)
                if answer.get("timeout"):
                    self._emit(stream, "error", {
                        "code": "ASK_TIMEOUT", "message": "提问超时未作答",
                        "recoverable": True})
                    return self._done(usage, tool_stats, rounds=round_no,
                                      charts=charts_emitted, stream=stream)
                messages.append({"role": "user", "content": f"（用户点选）{answer['choice']}"})
                continue

            if resp.tool_calls:
                messages.append(self._assistant_tool_msg(resp))
                await self._dispatch_tools(
                    resp, messages, task, stream, tool_stats, charts_emitted)
                continue

            # 终态：文本回复
            text_answer = resp.text
            break

        else:
            msg = "轮次上限(12)，任务未完成"
            self._emit(stream, "error", {
                "code": "MAX_ROUNDS", "message": msg, "recoverable": False})
            return self._done(usage, tool_stats, rounds=MAX_ROUNDS, error=msg,
                              charts=charts_emitted, stream=stream)

        done = self._done(usage, tool_stats, rounds=round_no, charts=charts_emitted,
                          stream=stream)
        done["final_text"] = text_answer
        done["final_report"] = self._last_report
        self._emit(stream, "done", {
            "usage": {"tokens_in": usage["tokens_in"], "tokens_out": usage["tokens_out"],
                      "calls": usage["calls"]},
            "cost_breakdown": {"total_cents": usage["cost_cents"],
                               "total_cny": round(usage["cost_cents"] / 100, 3)},
            "rounds": done["rounds"], "tool_stats": tool_stats,
            "charts": charts_emitted,
        })
        return done

    # ---------- 组装 ----------

    def _assemble(self, task: ChatTask) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if task.history_block:
            messages.append({"role": "system", "content": task.history_block})
        messages.extend(task.history)
        messages.append({"role": "user", "content": task.user_message})
        return messages

    def _stream_cb(self, stream: SessionStream):
        async def cb(channel: str, delta: str) -> None:
            self._emit(stream, channel, {"text": delta})
        return cb

    def _emit(self, stream: SessionStream, event: str, payload: dict) -> None:
        stream.publish(event, payload)

    # ---------- 工具调度 ----------

    def _assistant_tool_msg(self, resp) -> dict:
        return {
            "role": "assistant",
            "content": resp.text or None,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in resp.tool_calls
            ],
        }

    async def _dispatch_tools(self, resp, messages, task, stream,
                              tool_stats, charts_emitted) -> None:
        """① 并行工具调用 + ② 压缩 + ③ 沙箱（失败自动重试 1 次）。"""
        parallel_names = self.registry.parallel_safe_names()
        calls = resp.tool_calls
        parallel = [tc for tc in calls if tc.name in parallel_names]
        serial = [tc for tc in calls if tc.name not in parallel_names]

        async def run_one(tc):
            t0 = time.perf_counter()
            self._emit(stream, "tool_call", {"id": tc.id, "name": tc.name,
                                             "status": "start", "args": tc.arguments})
            result = await self.registry.execute(tc.name, tc.arguments)
            if not result.get("ok", True):
                # 自动重试 1 次上限
                result2 = await self.registry.execute(tc.name, tc.arguments)
                if result2.get("ok", True):
                    result = result2
                else:
                    tool_stats["errors"] += 1
            ms = int((time.perf_counter() - t0) * 1000)
            tool_stats["calls"] += 1
            tool_stats["ms_total"] += ms
            # ④ 结构化输出：报告以工具调用形式提交
            if tc.name == "submit_research_report" and result.get("ok"):
                self._last_report = result.get("report")
            compact = compact_result(tc.name, result)
            self._emit(stream, "tool_call", {
                "id": tc.id, "name": tc.name, "status": "success" if result.get("ok") else "error",
                "args": tc.arguments, "result_digest": self._digest(compact),
                "ms": ms, "cached": bool(result.get("cached"))})
            # 富组件提取（chart 事件独立下发）
            for chart in self._charts_from(tc.name, result):
                charts_emitted.append(chart["id"])
                self._emit(stream, "chart", chart)
            return {"role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(compact, ensure_ascii=False, default=str)}

        results = []
        if parallel:
            results.extend(await asyncio.gather(*[run_one(tc) for tc in parallel]))
        for tc in serial:  # 搜索类串行防限速
            results.append(await run_one(tc))
        messages.extend(results)

    def _digest(self, compact: dict) -> str:
        s = json.dumps(compact, ensure_ascii=False, default=str)
        return s[:160] + ("…" if len(s) > 160 else "")

    # ---------- ask 检测 / 图表提取 ----------

    def _detect_ask(self, resp) -> dict | None:
        """挂起类工具调用拦截：解析参数为 question/options。"""
        for tc in resp.tool_calls:
            if tc.name in ASK_TOOLS:
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                return {"question": args.get("question", "请选择"),
                        "options": args.get("options", []),
                        "context": args.get("context", {})}
        return None

    def _charts_from(self, tool_name: str, result: dict) -> list[dict]:
        """工具结果 → chart 富组件事件（kind 路由见前端）。"""
        charts: list[dict] = []
        if not result.get("ok"):
            return charts
        if tool_name == "portfolio_optimize" and result.get("frontier"):
            charts.append({
                "id": f"eff_{int(time.time())}", "kind": "eff_frontier",
                "title": f"有效前沿（{result.get('level')}）",
                "data": {"frontier": result["frontier"], "point": result["point"]},
                "cite": []})
        if tool_name == "get_kline" and result.get("bars"):
            charts.append({
                "id": f"kline_{result.get('symbol')}_{int(time.time())}",
                "kind": "kline", "title": f"{result.get('symbol')} K线",
                "data": {"bars": result["bars"][-120:]}, "cite": []})
        if tool_name == "get_correlation" and result.get("matrix"):
            charts.append({
                "id": f"corr_{int(time.time())}", "kind": "heatmap",
                "title": "相关性矩阵（近120日）", "data": result["matrix"], "cite": []})
        if tool_name == "walk_forward" and result.get("folds"):
            charts.append({
                "id": f"decay_{int(time.time())}", "kind": "decay",
                "title": "Walk-forward 逐折衰减",
                "data": {"folds": result["folds"]}, "cite": []})
        return charts

    def _done(self, usage, tool_stats, *, rounds, error="", charts=None, stream=None) -> dict:
        return {"rounds": rounds, "usage": usage, "tool_stats": tool_stats,
                "error": error, "charts": charts or [],
                "disclaimer": DISCLAIMER,
                "prompt_tokens_est": estimate_tokens("")}
