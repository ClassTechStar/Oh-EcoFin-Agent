"""多空辩论机制（项目的学术灵魂）。

流程（TradingAgents 核心机制的课程级复现：analyst → bull/bear → risk review → synthesis）：
Step 1 分析师（1 个 ReAct 循环，primary 模型）：并行调用行情/财务/新闻/估值/指标工具，
      产出带编号溯源的结构化 DataPack（含 data_as_of）；
Step 2 辩论轮（debate_rounds × economy 模型，稳定前缀 + DataPack 置尾 → 缓存命中价）：
      多方研究员论证看多 / 空方研究员论证看空（每个论据必须引用 [n]）；
      风控经理（1 次）：审查双方论据数据支撑度，输出 JSON {supported, unsupported, concerns}；
Step 3 综合研判（1 次 primary，submit_research_report 工具提交）→ Schema 校验。

深度优化：
- 辩论 prompt 稳定前缀 + 变量后缀（DataPack 序列化置尾）最大化 DeepSeek 缓存命中；
- 风控 unsupported 论据在综合轮 prompt 中强制降权；
- 每位辩手调用 token/成本记入 done 事件分角色明细——辩论成本透明。
降级预案：debate_rounds=1（多空合并 role prompting 区分视角）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from backend.engine.committee import (
    CommitteeTranscript,
    build_context,
)
from backend.engine.events import SessionStream
from backend.engine.gateway import LLMGateway
from backend.engine.prompts import (
    ANALYST_PROMPT,
    BEAR_PROMPT,
    BULL_PROMPT,
    DISCLAIMER,
    RISK_REVIEW_PROMPT,
    SYNTHESIS_PROMPT,
)
from backend.memory.summarize import estimate_tokens
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DP_TAIL_RE = re.compile(r"DATA:\s*$", re.MULTILINE)


class DebateOrchestrator:
    def __init__(self, gateway: LLMGateway, registry: ToolRegistry,
                 debate_rounds: int = 2, *,
                 analyst_max_rounds: int = 8,
                 analyst_tool_token_budget: int = 24000):
        self.gw = gateway
        self.registry = registry
        self.debate_rounds = debate_rounds
        # 分析师阶段预算护栏（与主 ReAct 的 RunBudget 同构）：防单次辩论吃掉当日成本帽
        self.analyst_max_rounds = analyst_max_rounds
        self.analyst_tool_token_budget = analyst_tool_token_budget

    # ---------- Step 1 分析师 ----------

    async def _analyst(self, session_id: str, symbols: list[str],
                       stream: SessionStream | None) -> tuple[str, list[dict], dict]:
        """分析师 ReAct 循环（轮次 + 工具 token 双预算）→ DataPack 文本。

        返回 (dp_text, raw_results, budget_used)；预算触顶时诚实截断并在 DataPack 尾注。
        """
        prompt = ANALYST_PROMPT.format(
            symbols=", ".join(symbols), data_as_of_hint="由工具返回的 data_as_of 注明")
        messages = [
            {"role": "system", "content": "你是投研分析师，只收集数据不判断。"},
            {"role": "user", "content": prompt},
        ]
        role_usage: dict = {"analyst": {"calls": 0, "cost_cents": 0}}
        raw_results: list[dict] = []  # 原始工具返回（溯源落库）
        tool_tokens = 0
        budget_used = {"rounds": 0, "tool_tokens": 0, "tool_budget_hit": False}

        for _ in range(self.analyst_max_rounds):
            budget_used["rounds"] += 1
            resp = await self.gw.chat(
                messages, tools=self.registry.schemas(),
                session_id=session_id, role="debate:analyst",
                stream_cb=(self._cb(stream) if stream else None))
            role_usage["analyst"]["calls"] += 1
            role_usage["analyst"]["cost_cents"] += resp.cost_cents
            if not resp.tool_calls:
                messages.append({"role": "assistant", "content": resp.text})
                break
            messages.append({
                "role": "assistant", "content": resp.text or None,
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.name, "arguments": tc.arguments}}
                               for tc in resp.tool_calls]})
            results = await asyncio.gather(*[
                self.registry.execute(tc.name, tc.arguments)
                for tc in resp.tool_calls], return_exceptions=True)
            for tc, res in zip(resp.tool_calls, results, strict=False):
                if isinstance(res, Exception):
                    res = {"ok": False, "error": str(res)}
                raw_results.append({"tool": tc.name, "args": tc.arguments,
                                    "result": res, "ts": time.strftime("%H:%M:%S")})
                content = json.dumps(res, ensure_ascii=False, default=str)
                tool_tokens += estimate_tokens(content)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": content})
            if tool_tokens >= self.analyst_tool_token_budget:
                # 工具结果预算触顶：停止取数，进入综合（诚实截断标记）
                budget_used["tool_budget_hit"] = True
                messages.append({
                    "role": "user",
                    "content": "（工具数据预算已用尽，请基于已收集数据立即产出 DataPack。）"})
                resp = await self.gw.chat(
                    messages, tools=[], session_id=session_id,
                    role="debate:analyst",
                    stream_cb=(self._cb(stream) if stream else None))
                role_usage["analyst"]["calls"] += 1
                role_usage["analyst"]["cost_cents"] += resp.cost_cents
                if resp.text:
                    messages.append({"role": "assistant", "content": resp.text})
                break
        budget_used["tool_tokens"] = tool_tokens
        # DataPack = 分析师最终文本；若无文本，回退为原始结果摘要
        dp_text = next((m["content"] for m in reversed(messages)
                        if m["role"] == "assistant" and m.get("content")), "")
        if not dp_text:
            dp_text = "\n".join(f"[{i+1}] {r['tool']} {r['result']}"
                                for i, r in enumerate(raw_results[:10]))
        if budget_used["tool_budget_hit"]:
            dp_text += "\n\n（注：分析师取数因工具预算触顶提前收束，以上为已收集部分。）"
        return dp_text, raw_results, budget_used

    def _cb(self, stream: SessionStream):
        async def cb(channel: str, delta: str) -> None:
            stream.publish(channel, {"text": delta})
        return cb

    # ---------- Step 2 辩论轮 ----------

    async def _debate_round(self, session_id: str, dp_text: str,
                            opponent_args: str, perspective: str) -> tuple[str, dict]:
        """单辩论角色一次调用（economy，稳定前缀缓存命中）。"""
        template = BULL_PROMPT if perspective == "bull" else BEAR_PROMPT
        prompt = template.replace("{dp}", "")
        content = prompt + f"\n\nDataPack:\n{dp_text}"
        if opponent_args:
            content += f"\n\n对方上一轮论据（可反驳）：\n{opponent_args}"
        text = await self.gw.simple(
            [{"role": "user", "content": content}], "economy",
            session_id=session_id, role=f"debate:{perspective}")
        return text, {}

    async def _debate_text(self, session_id: str, dp_text: str,
                           opponent_args: str, perspective: str) -> str:
        text, _ = await self._debate_round(session_id, dp_text, opponent_args, perspective)
        return text

    async def _risk_review(self, session_id: str, dp_text: str,
                           bull: str, bear: str) -> tuple[dict, str]:
        """风控经理：submit_risk_review 强约束提交（Schema 违例重试 1 次）+ 原始文本留痕。

        升级路径：正则提 JSON → Function Calling Schema 约束（与 submit_research_report
        同款纪律）；未走工具时保留正则兜底，解析失败原样返回（verification_required 抬升）。
        """
        prompt = RISK_REVIEW_PROMPT.format(dp="", bull=bull, bear=bear)
        content = prompt + f"\n\nDataPack:\n{dp_text}"
        messages = [{"role": "user", "content": content}]
        raw_text = ""
        for _attempt in range(2):
            resp = await self.gw.chat(
                messages, tools=self.registry.schemas(["submit_risk_review"]),
                session_id=session_id, role="debate:risk")
            raw_text = resp.text or raw_text
            if resp.tool_calls:
                tc = next((t for t in resp.tool_calls
                           if t.name == "submit_risk_review"), None)
                if tc:
                    result = await self.registry.execute("submit_risk_review", tc.arguments)
                    if result.get("ok"):
                        review = result["risk_review"]
                        # 原文留痕：文本为空时以工具入参 JSON 为准（Function Calling 的真实输出）
                        raw = resp.text or tc.arguments or json.dumps(
                            review, ensure_ascii=False)
                        return review, raw
                    # Schema 违例：带错误重试一次
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [{"id": tc.id, "type": "function",
                                        "function": {"name": tc.name,
                                                     "arguments": tc.arguments}}]})
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(result, ensure_ascii=False)})
                    continue
            break  # 未走工具 → 正则兜底
        return self._parse_risk_json(raw_text), raw_text

    def _parse_risk_json(self, text: str) -> dict:
        """容错解析风控 JSON。"""
        m = re.search(r"\{[\s\S]*\}", text or "")
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return {
                        "supported": data.get("supported", []),
                        "unsupported": data.get("unsupported", []),
                        "concerns": data.get("concerns", []),
                    }
            except json.JSONDecodeError:
                pass
        return {"supported": [], "unsupported": [], "concerns": ["风控 JSON 解析失败"]}

    # ---------- Step 3 综合研判 ----------

    async def _synthesize(self, session_id: str, symbols: list[str], dp_text: str,
                          bull: str, bear: str, risk: dict) -> tuple[dict, list[dict]]:
        """primary 模型 + submit_research_report 强约束提交。"""
        unsupported = risk.get("unsupported", [])
        synthesis = SYNTHESIS_PROMPT + (
            f"\n\n风控标记为无数据支撑、禁止作为结论依据的编号: {unsupported}" if unsupported else "")
        user = (f"标的: {', '.join(symbols)}\n\nDataPack:\n{dp_text}\n\n"
                f"多方论据:\n{bull}\n\n空方论据:\n{bear}\n\n风控意见:\n"
                f"{json.dumps(risk, ensure_ascii=False)}")
        report_raw: dict | None = None
        messages = [
            {"role": "system", "content": synthesis},
            {"role": "user", "content": user},
        ]
        tool_log: list[dict] = []
        for _attempt in range(2):  # Schema 违例自动带错误重试 1 次
            resp = await self.gw.chat(
                messages, tools=self.registry.schemas(["submit_research_report"]),
                session_id=session_id, role="debate:synthesis")
            if resp.tool_calls:
                tc = next((t for t in resp.tool_calls
                           if t.name == "submit_research_report"), None)
                if tc:
                    result = await self.registry.execute("submit_research_report", tc.arguments)
                    tool_log.append({"tool": "submit_research_report",
                                     "args": tc.arguments, "result": result})
                    if result.get("ok"):
                        report_raw = result["report"]
                        break
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [{"id": tc.id, "type": "function",
                                        "function": {"name": tc.name,
                                                     "arguments": tc.arguments}}]})
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(result, ensure_ascii=False)})
                    continue
            # 未走工具 → 兜底包装
            report_raw = self._fallback_report(symbols, resp.text)
            break
        if report_raw is None:
            report_raw = self._fallback_report(symbols, "")
        return report_raw, tool_log

    def _fallback_report(self, symbols: list[str], text: str) -> dict:
        """综合轮未按 Schema 提交时的兜底最小报告。"""
        from backend.data.calendar import now_cst

        now = now_cst().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "meta": {"symbol": symbols[0], "name": "", "data_as_of": now,
                     "generated_at": now},
            "sections": [{"type": "text", "title": "综合研判", "text": text or "无内容"}],
            "citations": [{"n": 1, "tool": "fallback", "args": "", "ts": now,
                           "excerpt": ""}],
            "verdict": "neutral",
            "thesis_digest": (text or "")[:80],
            "risk_disclosure": DISCLAIMER,
        }

    # ---------- 主入口 ----------

    async def _run_role(self, transcript: CommitteeTranscript, role: str, rnd: int,
                        call) -> str:
        """隔离执行单个角色：失败落痕并返回空串，绝不伪造中性意见。"""
        t0 = time.perf_counter()
        try:
            text = await call()
        except Exception as e:  # 网络/模型失败必须可见
            logger.exception("辩论角色 %s r%s 失败", role, rnd)
            transcript.failed_turn(
                role, rnd, f"{type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t0) * 1000))
            return ""
        if not (text or "").strip():
            transcript.failed_turn(
                role, rnd, "角色返回空文本（不视为中性意见）",
                latency_ms=int((time.perf_counter() - t0) * 1000))
            return ""
        transcript.ok_turn(role, rnd, text,
                           latency_ms=int((time.perf_counter() - t0) * 1000))
        return text

    @staticmethod
    def _analysis_as_of(raw_results: list[dict]) -> str:
        """证据时点：优先取工具返回的 data_as_of，缺失才回落到当前时刻。"""
        for r in reversed(raw_results):
            res = r.get("result")
            if isinstance(res, dict) and res.get("data_as_of"):
                return str(res["data_as_of"])
        from backend.data.calendar import now_cst

        return now_cst().strftime("%Y-%m-%d %H:%M:%S")

    async def run(self, session_id: str, symbols: list[str],
                  user_question: str, stream: SessionStream | None = None,
                  *, resume_datapack: str | None = None) -> dict:
        """辩论主入口。

        resume_datapack：checkpoint 续跑——Step 2/3 失败时从已固化的 DataPack 重试，
        不再全链重跑（辩论是最贵链路；断网/模型抖动的现场救命机制）。
        """
        t0 = time.perf_counter()
        resumed = bool((resume_datapack or "").strip())
        budget_used: dict = {"rounds": 0, "tool_tokens": 0, "tool_budget_hit": False}

        # Step 1（checkpoint 命中则跳过分析师取数）
        if resumed:
            dp_text = resume_datapack
            raw_results: list[dict] = []  # 原始返回不可再得：诚实留痕，溯源以 checkpoint 为准
        else:
            dp_text, raw_results, budget_used = await self._analyst(
                session_id, symbols, stream)
        # 溯源落库（原始返回为准）
        if stream is not None:
            for r in raw_results:
                stream.publish("tool_call", {
                    "id": f"dp_{r['tool']}_{int(time.time() * 1000)}",
                    "name": r["tool"], "status": "success",
                    "args": r["args"][:120], "result_digest": str(r["result"])[:160],
                    "ms": 0, "cached": False})

        # 不可变上下文快照：所有角色只读同一事实集（Stage 1）
        ctx = build_context(symbols, raw_results,
                            self._analysis_as_of(raw_results), user_question)
        transcript = CommitteeTranscript(context=ctx)
        transcript.ok_turn("analyst", 0, dp_text,
                           note="checkpoint 恢复（跳过分析师取数）" if resumed else "")

        # Step 2 辩论（隔离角色 + 失败可见 + 原文留痕）
        bull, bear = "", ""
        rounds_used = 0
        for i in range(max(self.debate_rounds, 1)):
            rounds_used += 1
            if self.debate_rounds == 1:
                # 降级：多空合并一次调用，role prompting 区分视角（ TradingAgents 1 轮结论仍成立）
                merged = (f"你先以多方研究员身份论证看多，再以空方研究员身份论证看空。\n"
                          f"输出 JSON: {{\"bull\": [...], \"bear\": [...]}}，"
                          f"每个论据引用 DataPack 编号 [n]。\n\nDataPack:\n{dp_text}")
                text = await self._run_role(
                    transcript, "merged", 1,
                    lambda: self.gw.simple(
                        [{"role": "user", "content": merged}], "economy",
                        session_id=session_id, role="debate:merged"))
                if text:
                    m = re.search(r"\{[\s\S]*\}", text)
                    if m:
                        try:
                            data = json.loads(m.group(0))
                            bull = json.dumps(data.get("bull", []), ensure_ascii=False)
                            bear = json.dumps(data.get("bear", []), ensure_ascii=False)
                        except json.JSONDecodeError:
                            bull, bear = text[:600], text[600:1200]
                    else:
                        bull, bear = text[:600], text[600:1200]
                    transcript.ok_turn("bull", 1, bull, merged=True)
                    transcript.ok_turn("bear", 1, bear, merged=True)
                else:
                    transcript.failed_turn("bull", 1, "多空合并调用失败", merged=True)
                    transcript.failed_turn("bear", 1, "多空合并调用失败", merged=True)
                break
            prev_bull, prev_bear = bull, bear
            bull = await self._run_role(
                transcript, "bull", i + 1,
                lambda: self._debate_text(session_id, dp_text,
                                          prev_bear if i else "", "bull"))
            bear = await self._run_role(
                transcript, "bear", i + 1,
                lambda: self._debate_text(session_id, dp_text,
                                          prev_bull if i else "", "bear"))

        # 风控复核（独立闸门；失败/解析失败都要抬升 verification_required）
        t_risk = time.perf_counter()
        try:
            risk, risk_raw = await self._risk_review(session_id, dp_text, bull, bear)
            transcript.ok_turn("risk", 1, risk_raw,
                               latency_ms=int((time.perf_counter() - t_risk) * 1000))
        except Exception as e:
            logger.exception("辩论角色 risk 失败")
            risk = {"supported": [], "unsupported": [],
                    "concerns": [f"风控角色失败: {e}"]}
            transcript.failed_turn(
                "risk", 1, f"{type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t_risk) * 1000))

        # Step 3 CIO 汇总（失败也保底出报告，但标记需人工复核）
        t_cio = time.perf_counter()
        try:
            report, _synth_log = await self._synthesize(session_id, symbols, dp_text,
                                                        bull, bear, risk)
            transcript.ok_turn(
                "cio", 0, json.dumps(report, ensure_ascii=False, default=str),
                latency_ms=int((time.perf_counter() - t_cio) * 1000))
        except Exception as e:
            logger.exception("辩论角色 cio 失败")
            report = self._fallback_report(symbols, "")
            transcript.failed_turn(
                "cio", 0, f"{type(e).__name__}: {e}",
                latency_ms=int((time.perf_counter() - t_cio) * 1000))

        # 失败可见：任何角色失败 / 风控无支撑 / 风控解析失败 → 必须复核
        if transcript.failed_turns():
            transcript.verification_required = True
            transcript.verification_notes.extend(
                f"{t.role} r{t.round} 失败: {t.error}" for t in transcript.failed_turns())
        if risk.get("unsupported"):
            transcript.verification_required = True
            transcript.verification_notes.append(
                f"风控标记无数据支撑编号: {risk['unsupported']}")
        if any("解析失败" in str(c) for c in risk.get("concerns", [])):
            transcript.verification_required = True
            transcript.verification_notes.append("风控 JSON 解析失败，需人工复核")

        latency_ms = int((time.perf_counter() - t0) * 1000)
        result = {
            "ok": True,
            "report": report,
            "datapack": dp_text,
            "resumed": resumed,
            "analyst_budget": budget_used,
            "context": ctx.to_audit_dict(),
            "transcript": transcript.to_audit_dict(),
            "verification_required": transcript.verification_required,
            "validation_issues": transcript.validate(),
            "debate": {"bull": bull, "bear": bear, "risk_review": risk,
                       "rounds": rounds_used, "context_id": ctx.context_id,
                       "failures": [t.to_dict() for t in transcript.failed_turns()]},
            "raw_tool_results": raw_results,
            "latency_ms": latency_ms,
            "disclaimer": DISCLAIMER,
        }
        if stream is not None:
            stream.publish("chart", {
                "id": f"debate_{int(time.time())}", "kind": "radar",
                "title": "多空辩论支撑度",
                "data": {"bull": bull[:200], "bear": bear[:200],
                         "supported": risk.get("supported", []),
                         "unsupported": risk.get("unsupported", []),
                         "verification_required": transcript.verification_required},
                "cite": []})
        return result
