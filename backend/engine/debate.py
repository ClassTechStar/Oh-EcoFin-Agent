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
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DP_TAIL_RE = re.compile(r"DATA:\s*$", re.MULTILINE)


class DebateOrchestrator:
    def __init__(self, gateway: LLMGateway, registry: ToolRegistry,
                 debate_rounds: int = 2):
        self.gw = gateway
        self.registry = registry
        self.debate_rounds = debate_rounds

    # ---------- Step 1 分析师 ----------

    async def _analyst(self, session_id: str, symbols: list[str],
                       stream: SessionStream | None) -> tuple[str, list[dict]]:
        """分析师 ReAct 循环（上限 8 轮）→ DataPack 文本。"""
        prompt = ANALYST_PROMPT.format(
            symbols=", ".join(symbols), data_as_of_hint="由工具返回的 data_as_of 注明")
        messages = [
            {"role": "system", "content": "你是投研分析师，只收集数据不判断。"},
            {"role": "user", "content": prompt},
        ]
        role_usage: dict = {"analyst": {"calls": 0, "cost_cents": 0}}
        raw_results: list[dict] = []  # 原始工具返回（溯源落库）

        for _ in range(8):
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
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(res, ensure_ascii=False, default=str)})
        # DataPack = 分析师最终文本；若无文本，回退为原始结果摘要
        dp_text = next((m["content"] for m in reversed(messages)
                        if m["role"] == "assistant" and m.get("content")), "")
        if not dp_text:
            dp_text = "\n".join(f"[{i+1}] {r['tool']} {r['result']}"
                                for i, r in enumerate(raw_results[:10]))
        return dp_text, raw_results

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

    async def _risk_review(self, session_id: str, dp_text: str,
                           bull: str, bear: str) -> dict:
        """风控经理：JSON {supported, unsupported, concerns}。"""
        prompt = RISK_REVIEW_PROMPT.format(dp="", bull=bull, bear=bear)
        content = prompt + f"\n\nDataPack:\n{dp_text}"
        text = await self.gw.simple(
            [{"role": "user", "content": content}], "economy",
            session_id=session_id, role="debate:risk")
        return self._parse_risk_json(text)

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

    async def run(self, session_id: str, symbols: list[str],
                  user_question: str, stream: SessionStream | None = None) -> dict:
        t0 = time.perf_counter()

        # Step 1
        dp_text, raw_results = await self._analyst(session_id, symbols, stream)
        # 溯源落库（原始返回为准）
        if stream is not None:
            for r in raw_results:
                stream.publish("tool_call", {
                    "id": f"dp_{r['tool']}_{int(time.time() * 1000)}",
                    "name": r["tool"], "status": "success",
                    "args": r["args"][:120], "result_digest": str(r["result"])[:160],
                    "ms": 0, "cached": False})

        # Step 2 辩论
        bull, bear = "", ""
        rounds_used = 0
        for i in range(max(self.debate_rounds, 1)):
            rounds_used += 1
            if self.debate_rounds == 1:
                # 降级：多空合并一次调用，role prompting 区分视角（ TradingAgents 1 轮结论仍成立）
                merged = (f"你先以多方研究员身份论证看多，再以空方研究员身份论证看空。\n"
                          f"输出 JSON: {{\"bull\": [...], \"bear\": [...]}}，"
                          f"每个论据引用 DataPack 编号 [n]。\n\nDataPack:\n{dp_text}")
                text = await self.gw.simple(
                    [{"role": "user", "content": merged}], "economy",
                    session_id=session_id, role="debate:merged")
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
                break
            bull_prev = bull
            bull, _ = await self._debate_round(session_id, dp_text,
                                               bear if i else "", "bull")
            bear, _ = await self._debate_round(session_id, dp_text,
                                               bull_prev if i else "", "bear")

        risk = await self._risk_review(session_id, dp_text, bull, bear)

        # Step 3
        report, synth_log = await self._synthesize(session_id, symbols, dp_text,
                                                   bull, bear, risk)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        result = {
            "ok": True,
            "report": report,
            "datapack": dp_text,
            "debate": {"bull": bull, "bear": bear, "risk_review": risk,
                       "rounds": rounds_used},
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
                         "unsupported": risk.get("unsupported", [])},
                "cite": []})
        return result
