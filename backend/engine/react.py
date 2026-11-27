"""ReAct 主循环 + 规划状态机（M6）。

一次 run 的生命周期：
planning → acting → observing → planning … → synthesizing → done
ask_user 挂起为 waiting_user；异常/预算耗尽 → failed。

关键纪律：
1. 一次 run 恰一个 `done` 终态事件；`error` 只做过程告警，不是终态；
2. 工具副作用分级（none/session/workspace/external），external/workspace 需
   用户消息中的明确意图才放行；被拦截的调用不执行、不自动重试；
3. 上下文两级压缩（工具结果 → 丢弃最老对话单元），仍超预算则确定性收尾；
4. 工具失败在 tool_call 事件、tool_stats 与 done 中均可见；
5. run 记录落 `agent_runs`，为 M7 证据/重跑提供 run_id。
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
from backend.engine.context import fit_context
from backend.engine.events import SessionStream
from backend.engine.gateway import LLMGateway, RecoverableLLMError
from backend.engine.prompts import DISCLAIMER, SYSTEM_PROMPT
from backend.engine.run_state import (
    TERMINAL_STATES,
    AgentRun,
    RunBudget,
    RunState,
)
from backend.engine.runs import RunStore
from backend.engine.tool_calls import ToolCallStore
from backend.memory.summarize import TOKENS_PER_CHAR, estimate_tokens
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_ROUNDS = 12

# 挂起类工具：引擎不执行、不回填，转为 ask_user 事件挂起
ASK_TOOLS = {"ask_risk_question"}

#: 明确交易意图（避免“值得买吗”这类咨询误触发下单）
TRADE_INTENT = (
    "下单", "买入", "卖出", "委托", "加仓", "减仓", "清仓", "交易",
    "帮我买", "帮我卖", "买一点", "卖一点", "全部卖出", "全部买入",
)
#: 明确报告意图
REPORT_INTENT = (
    "生成报告", "出报告", "导出报告", "做一份报告", "做个报告", "做报告",
    "写报告", "报告导出", "生成一份报告", "生成个报告", "出一份报告",
)
#: 明确自由代码意图（避免“帮我算一下”这类泛化请求触发沙箱）
SANDBOX_INTENT = (
    "运行代码", "执行代码", "跑代码", "跑一段代码", "跑一下代码", "写代码",
    "代码计算", "用代码", "自定义代码", "自由代码", "沙箱", "python 代码",
    "python代码", "Python代码", "python 脚本", "Python脚本",
)


@dataclass
class ChatTask:
    session_id: str
    user_message: str
    history: list[dict] = field(default_factory=list)   # [{role, content}]
    history_block: str = ""                             # 决策记忆注入块
    mode: str = "chat"                                  # chat | debate | advisor


def _truncate_tool_text(text: str, token_limit: int) -> str:
    max_chars = max(200, int(token_limit / max(TOKENS_PER_CHAR, 0.01)))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…[工具结果已按预算截断]"


class ReActEngine:
    def __init__(self, gateway: LLMGateway, registry: ToolRegistry,
                 ask_manager: AskUserManager, *,
                 budget: RunBudget | None = None,
                 run_store: RunStore | None = None,
                 tool_store: ToolCallStore | None = None):
        self.gw = gateway
        self.registry = registry
        self.ask_manager = ask_manager
        self.budget = budget or RunBudget(max_rounds=MAX_ROUNDS)
        self.run_store = run_store
        self.tool_store = tool_store

    # ---------- 主循环 ----------

    async def run(self, task: ChatTask, stream: SessionStream) -> dict:
        """执行一次完整会话轮；返回 done payload（同时发且只发一次 done 事件）。"""
        run = AgentRun.create(task.session_id, task.mode)
        if self.run_store is not None:
            try:
                self.run_store.start(run)
            except Exception:
                logger.exception("agent_runs 起始记录失败")
        self._emit_state(run, stream, previous=None)

        messages = self._assemble(task)
        usage = {"tokens_in": 0, "tokens_out": 0, "cost_cents": 0, "calls": 0}
        tool_stats: dict[str, Any] = {
            "calls": 0, "errors": 0, "blocked": 0, "truncated": 0,
            "cached": 0, "ms_total": 0, "tokens": 0,
        }
        charts_emitted: list[str] = []
        text_answer = ""
        last_text = ""
        last_context_tokens = 0
        self._last_report: dict | None = None

        try:
            for round_no in range(1, self.budget.max_rounds + 1):
                run.rounds = round_no
                if run.state != RunState.PLANNING:
                    self._transition(run, RunState.PLANNING, stream)

                fit = fit_context(
                    messages,
                    context_tokens=self.budget.context_tokens,
                    tool_result_tokens=self.budget.tool_result_tokens,
                    keep_recent_messages=self.budget.keep_recent_messages,
                )
                last_context_tokens = fit.used_after
                if fit.compressed:
                    self._emit(stream, "budget", {
                        "run_id": run.run_id, "action": "compressed",
                        "used_before": fit.used_before, "used_after": fit.used_after,
                        "budget": self.budget.context_tokens,
                        "tool_compressed": fit.tool_compressed,
                        "dropped_units": fit.dropped_units,
                    })
                messages = fit.messages
                if fit.over_budget:
                    message = "上下文预算已用尽，本次任务提前收尾"
                    self._emit(stream, "budget", {
                        "run_id": run.run_id, "action": "wrap_up",
                        "used": fit.used_after, "budget": self.budget.context_tokens,
                        "message": message,
                    })
                    self._emit(stream, "error", {
                        "code": "CONTEXT_BUDGET", "message": message,
                        "recoverable": False})
                    return self._finish(
                        run, stream, usage, tool_stats,
                        stop_reason="context_budget", error=message,
                        final_text=self._wrap_up(last_text), charts=charts_emitted,
                        truncated=True, context_tokens=last_context_tokens)

                try:
                    resp = await self.gw.chat(
                        messages, tools=self.registry.schemas(),
                        stream_cb=self._stream_cb(stream), session_id=task.session_id,
                        role=f"react:{task.mode}",
                    )
                except RecoverableLLMError as e:
                    self._emit(stream, "error", {
                        "code": "LLM_UNAVAILABLE", "message": str(e),
                        "recoverable": True})
                    return self._finish(
                        run, stream, usage, tool_stats,
                        stop_reason="llm_unavailable", error=str(e),
                        final_text=last_text, charts=charts_emitted,
                        context_tokens=last_context_tokens)
                except Exception as e:  # 成本熔断等不可恢复
                    self._emit(stream, "error", {
                        "code": type(e).__name__, "message": str(e),
                        "recoverable": False})
                    return self._finish(
                        run, stream, usage, tool_stats,
                        stop_reason="llm_error", error=str(e),
                        final_text=last_text, charts=charts_emitted,
                        context_tokens=last_context_tokens)

                usage["tokens_in"] += resp.usage_in
                usage["tokens_out"] += resp.usage_out
                usage["cost_cents"] += resp.cost_cents
                usage["calls"] += 1
                last_text = resp.text or last_text

                if resp.tool_calls and (ask := self._detect_ask(resp)):
                    # ask_user 挂起：不执行、不回填，事件挂起等待点选
                    self._transition(run, RunState.WAITING_USER, stream)
                    pa = await self.ask_manager.create(
                        task.session_id, ask["question"], ask.get("options", []),
                        ask.get("context"))
                    self._emit(stream, "ask_user", {
                        "qid": pa.qid, "question": pa.question,
                        "options": pa.options, "context": pa.context})
                    answer = await self.ask_manager.wait(pa)
                    if answer.get("timeout"):
                        message = "提问超时未作答"
                        self._emit(stream, "error", {
                            "code": "ASK_TIMEOUT", "message": message,
                            "recoverable": True})
                        return self._finish(
                            run, stream, usage, tool_stats,
                            stop_reason="ask_timeout", error=message,
                            final_text=last_text, charts=charts_emitted,
                            context_tokens=last_context_tokens)
                    messages.append({
                        "role": "user",
                        "content": f"（用户点选）{answer['choice']}"})
                    self._transition(run, RunState.PLANNING, stream)
                    continue

                if resp.tool_calls:
                    self._transition(run, RunState.ACTING, stream)
                    messages.append(self._assistant_tool_msg(resp))
                    await self._dispatch_tools(
                        resp, messages, task, stream, tool_stats,
                        charts_emitted, run)
                    self._transition(run, RunState.OBSERVING, stream)
                    continue

                # 终态：文本回复
                text_answer = resp.text
                self._transition(run, RunState.SYNTHESIZING, stream)
                return self._finish(
                    run, stream, usage, tool_stats, stop_reason="answer",
                    final_text=text_answer, charts=charts_emitted,
                    context_tokens=last_context_tokens)

            message = f"轮次上限({self.budget.max_rounds})，任务未完成"
            self._emit(stream, "error", {
                "code": "MAX_ROUNDS", "message": message, "recoverable": False})
            return self._finish(
                run, stream, usage, tool_stats, stop_reason="max_rounds",
                error=message, final_text=self._wrap_up(last_text),
                charts=charts_emitted, truncated=True,
                context_tokens=last_context_tokens)
        except Exception as e:
            logger.exception("ReAct 运行异常")
            self._emit(stream, "error", {
                "code": "INTERNAL", "message": f"引擎异常: {e}",
                "recoverable": False})
            return self._finish(
                run, stream, usage, tool_stats, stop_reason="internal_error",
                error=str(e), final_text=self._wrap_up(last_text),
                charts=charts_emitted, truncated=True,
                context_tokens=last_context_tokens)
        finally:
            if self.run_store is not None:
                try:
                    self.run_store.finish(run, summary=run.summary)
                except Exception:
                    logger.exception("agent_runs 结束记录失败")

    # ---------- 状态与终态 ----------

    def _emit_state(self, run: AgentRun, stream: SessionStream,
                    previous: str | None) -> None:
        self._emit(stream, "run_state", {
            "run_id": run.run_id, "state": run.state.value,
            "from": previous, "rounds": run.rounds,
        })

    def _transition(self, run: AgentRun, next_state: RunState,
                    stream: SessionStream) -> None:
        previous = run.state.value
        run.transition(next_state)
        self._emit_state(run, stream, previous)

    def _finish(self, run: AgentRun, stream: SessionStream, usage: dict,
                tool_stats: dict, *, stop_reason: str, error: str = "",
                final_text: str = "", charts: list | None = None,
                truncated: bool = False, context_tokens: int = 0) -> dict:
        """唯一的终态出口：保证一次 run 只发一个 done。"""
        if run.terminal_emitted and run.done_payload is not None:
            return run.done_payload

        target = (RunState.DONE if stop_reason in ("answer", "context_budget")
                  else RunState.FAILED)
        if target == RunState.DONE:
            if run.state != RunState.SYNTHESIZING:
                if run.can(RunState.SYNTHESIZING):
                    self._transition(run, RunState.SYNTHESIZING, stream)
                else:
                    target = RunState.FAILED
            if target == RunState.DONE:
                self._transition(run, RunState.DONE, stream)
        if target == RunState.FAILED and run.state not in TERMINAL_STATES:
            self._transition(run, RunState.FAILED, stream)

        run.stop_reason = stop_reason
        run.error = error
        run.summary = final_text[:500]
        run.tokens_in = usage["tokens_in"]
        run.tokens_out = usage["tokens_out"]
        run.cost_cents = usage["cost_cents"]
        run.terminal_emitted = True

        payload = {
            "run_id": run.run_id,
            "terminal": True,
            "state": run.state.value,
            "stop_reason": stop_reason,
            "rounds": run.rounds,
            "usage": {
                "tokens_in": usage["tokens_in"],
                "tokens_out": usage["tokens_out"],
                "calls": usage["calls"],
            },
            "cost_breakdown": {
                "total_cents": usage["cost_cents"],
                "total_cny": round(usage["cost_cents"] / 100, 3),
            },
            "tool_stats": tool_stats,
            "charts": charts or [],
            "error": error,
            "truncated": truncated,
            "final_text": final_text,
            "final_report": self._last_report,
            "budget": {
                "context_tokens": self.budget.context_tokens,
                "tool_result_tokens": self.budget.tool_result_tokens,
                "total_tool_tokens": self.budget.total_tool_tokens,
                "context_used": context_tokens,
            },
            "disclaimer": DISCLAIMER,
            "prompt_tokens_est": context_tokens,
        }
        run.done_payload = payload
        self._emit(stream, "done", payload)
        return payload

    def _wrap_up(self, last_text: str) -> str:
        if last_text:
            return last_text + "\n\n（上下文预算已用尽，以上为阶段性结论。）"
        return "已达到上下文预算上限，已停止继续取数。请缩小问题范围或分步提问。"

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

    def _allow_side_effect(self, tool_name: str, user_message: str) -> bool:
        spec = self.registry.get(tool_name)
        if spec is None or spec.confirmation != "explicit_request":
            return True
        if spec.side_effect == "workspace":
            if tool_name == "run_sandbox_code":
                return any(keyword in user_message for keyword in SANDBOX_INTENT)
            if any(keyword in user_message for keyword in REPORT_INTENT):
                return True
            return ("报告" in user_message
                    and any(verb in user_message for verb in
                            ("生成", "导出", "做", "写", "出", "整理")))
        if spec.side_effect == "external":
            return any(keyword in user_message for keyword in TRADE_INTENT)
        return True

    async def _dispatch_tools(self, resp, messages, task, stream,
                              tool_stats, charts_emitted,
                              run: AgentRun) -> None:
        """并行工具调用 + 压缩 + 沙箱 + 副作用确认（失败自动重试至多 1 次）。"""
        parallel_names = self.registry.parallel_safe_names()
        calls = resp.tool_calls
        parallel = [tc for tc in calls if tc.name in parallel_names]
        serial = [tc for tc in calls if tc.name not in parallel_names]

        async def run_one(tc):
            t0 = time.perf_counter()
            spec = self.registry.get(tc.name)
            allow = self._allow_side_effect(tc.name, task.user_message)
            self._emit(stream, "tool_call", {
                "id": tc.id, "name": tc.name, "status": "start",
                "args": tc.arguments,
                "side_effect": getattr(spec, "side_effect", "none"),
                "confirmation": getattr(spec, "confirmation", "none"),
            })
            result = await self.registry.execute(
                tc.name, tc.arguments, allow_side_effects=allow)
            requires_confirmation = bool(result.get("requires_confirmation"))
            can_retry = spec is None or spec.side_effect in ("none", "session")
            if (not result.get("ok", True) and not requires_confirmation
                    and can_retry):
                result2 = await self.registry.execute(
                    tc.name, tc.arguments, allow_side_effects=allow)
                if result2.get("ok", True):
                    result = result2

            ms = int((time.perf_counter() - t0) * 1000)
            if self.tool_store is not None:
                try:
                    self.tool_store.record(
                        task.session_id, tc.name, tc.arguments, result, ms,
                        bool(result.get("ok")), bool(result.get("cached")))
                except Exception:
                    logger.exception("工具原始结果落库失败")
            run.tool_calls += 1
            tool_stats["calls"] += 1
            tool_stats["ms_total"] += ms
            if requires_confirmation:
                run.tool_blocked += 1
                tool_stats["blocked"] += 1
                self._emit(stream, "error", {
                    "code": "CONFIRMATION_REQUIRED",
                    "message": result.get("error", "需要用户明确授权"),
                    "recoverable": True, "tool": tc.name,
                })
            elif not result.get("ok", True):
                run.tool_errors += 1
                tool_stats["errors"] += 1
            if result.get("cached"):
                tool_stats["cached"] += 1

            # ④ 结构化输出：报告以工具调用形式提交
            if tc.name == "submit_research_report" and result.get("ok"):
                self._last_report = result.get("report")
            compact = compact_result(tc.name, result)
            content = json.dumps(compact, ensure_ascii=False, default=str)
            tokens = estimate_tokens(content)
            if tokens > self.budget.tool_result_tokens:
                content = _truncate_tool_text(content, self.budget.tool_result_tokens)
                tokens = estimate_tokens(content)
                tool_stats["truncated"] += 1
            tool_stats["tokens"] += tokens

            self._emit(stream, "tool_call", {
                "id": tc.id, "name": tc.name,
                "status": "success" if result.get("ok") else "error",
                "args": tc.arguments,
                "result_digest": self._digest(compact),
                "error": result.get("error", ""),
                "hint": result.get("hint", ""),
                "requires_confirmation": requires_confirmation,
                "side_effect": getattr(spec, "side_effect", "none"),
                "ms": ms, "cached": bool(result.get("cached")),
                "truncated": tokens >= self.budget.tool_result_tokens,
            })
            for chart in self._charts_from(tc.name, result):
                charts_emitted.append(chart["id"])
                self._emit(stream, "chart", chart)
            return {"role": "tool", "tool_call_id": tc.id, "content": content}

        results = []
        if parallel:
            results.extend(await asyncio.gather(*[run_one(tc) for tc in parallel]))
        for tc in serial:  # 搜索/写操作串行防限速与并发副作用
            results.append(await run_one(tc))
        messages.extend(results)

        if (tool_stats["tokens"] > self.budget.total_tool_tokens
                and not tool_stats.get("budget_notified")):
            tool_stats["budget_notified"] = True
            self._emit(stream, "budget", {
                "run_id": run.run_id, "action": "tool_budget",
                "used": tool_stats["tokens"],
                "budget": self.budget.total_tool_tokens,
            })

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
