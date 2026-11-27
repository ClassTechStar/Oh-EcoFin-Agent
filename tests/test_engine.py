"""ReAct 引擎 + 辩论编排器（Mock 网关剧本回放，CI 零密钥）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.engine.askuser import AskUserManager
from backend.engine.debate import DebateOrchestrator
from backend.engine.events import SessionStream
from backend.engine.gateway import LLMResult, ToolCall
from backend.engine.react import MAX_ROUNDS, ChatTask, ReActEngine
from backend.tools.registry import ToolRegistry, ToolSpec
from pydantic import BaseModel, ConfigDict, Field


class P(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(..., description="s")


class MockGateway:
    """剧本回放网关。"""

    def __init__(self, script=None, simple_texts=None):
        self.script = list(script or [])
        self.simple_texts = simple_texts or {}
        self.chat_calls = []

    async def chat(self, messages, tools=None, tier="primary", *, session_id="",
                   role="chat", stream_cb=None, temperature=0.3):
        self.chat_calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        step = self.script.pop(0) if self.script else {"text": "剧本耗尽"}
        text = step.get("text", "")
        if text and stream_cb:
            await stream_cb("message_delta", text)
        tcs = [ToolCall(id=f"t{i}", name=n, arguments=json.dumps(a, ensure_ascii=False))
               for i, (n, a) in enumerate(step.get("tool_calls", []))]
        return LLMResult(text=text, tool_calls=tcs,
                         usage_in=10, usage_out=5, cost_cents=1,
                         model="fake", tier=tier)

    async def simple(self, messages, tier="economy", **kw):
        content = str(messages[-1]["content"])
        for k, v in self.simple_texts.items():
            if content.startswith(k):
                return v
        return self.simple_texts.get("*", "默认")


def build_registry():
    reg = ToolRegistry()
    calls = []

    async def h_quote(symbol):
        calls.append(("get_quote", symbol))
        return {"ok": True, "symbol": symbol, "price": 100.0, "data_as_of": "2026-11-27 10:00"}

    async def h_report(report):
        return {"ok": True, "report": report}

    reg.register(ToolSpec("get_quote", "q", P, h_quote, parallel_safe=True))
    return reg, calls


def build_engine(gateway, reg):
    from backend.db import Database
    import tempfile

    db = Database(tempfile.mktemp(suffix=".db"))
    mgr = AskUserManager(db, timeout_seconds=5)
    return ReActEngine(gateway, reg, mgr)


async def test_react_parallel_and_final_text():
    reg, calls = build_registry()
    gw = MockGateway(script=[
        {"tool_calls": [("get_quote", {"symbol": "600519"})]},
        {"tool_calls": [("get_quote", {"symbol": "000001"})]},
        {"text": "两只股票都看完了。"},
    ])
    eng = build_engine(gw, reg)
    stream = SessionStream("t")
    done = await eng.run(ChatTask("t", "帮我看看 600519 和 000001"), stream)
    assert done["final_text"] == "两只股票都看完了。"
    assert done["rounds"] == 3
    assert [c[1] for c in calls] == ["600519", "000001"]
    # 事件流包含 tool_call 与 message_delta
    kinds = [e.event for e in stream._buffer]
    assert "tool_call" in kinds and "message_delta" in kinds and "done" in kinds


async def test_react_tool_result_compacted_into_messages():
    reg, _ = build_registry()
    gw = MockGateway(script=[
        {"tool_calls": [("get_quote", {"symbol": "600519"})]},
        {"text": "done"},
    ])
    eng = build_engine(gw, reg)
    stream = SessionStream("t2")
    await eng.run(ChatTask("t2", "查询"), stream)
    msgs = gw.chat_calls[1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = json.loads(tool_msgs[0]["content"])
    assert content["kind"] == "quote"  # 压缩器生效（snapshot 档）
    assert "price" in content
    # assistant 消息携带原始 tool_calls
    asst = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst and asst[0]["tool_calls"][0]["function"]["name"] == "get_quote"


async def test_react_sandbox_violation_retry_once():
    """违例参数不抛异常 → 回错误提示 → 自动重试 1 次上限。"""
    reg, calls = build_registry()
    gw = MockGateway(script=[
        {"tool_calls": [("get_quote", {"symbol": "!!bad"})]},   # 违例
        {"tool_calls": [("get_quote", {"symbol": "600519"})]},  # 模型自纠
        {"text": "ok"},
    ])
    eng = build_engine(gw, reg)
    stream = SessionStream("t3")
    done = await eng.run(ChatTask("t3", "查询"), stream)
    assert done["final_text"] == "ok"
    assert ("get_quote", "600519") in calls


async def test_react_ask_user_suspend_resume():
    """ask_user 挂起 → 用户点选 → 续跑（askuser 注入答案）。"""
    reg, calls = build_registry()

    class AskP(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str = Field(..., description="q")
        options: list[str] = Field(..., min_length=2)

    async def h_ask(question, options, **kw):
        return {"ok": False}

    reg.register(ToolSpec("ask_risk_question", "ask", AskP, h_ask))
    gw = MockGateway(script=[
        {"tool_calls": [("ask_risk_question", {"question": "能接受回撤吗？",
                                               "options": ["能", "不能"]})]},
        {"text": "收到您的选择。"},
    ])
    eng = build_engine(gw, reg)
    stream = SessionStream("t4")
    run_task = asyncio.create_task(
        eng.run(ChatTask("t4", "测评"), stream))
    await asyncio.sleep(0.1)
    # 找到 ask_user 事件中的 qid 并 resume
    ask_events = [e for e in stream._buffer if e.event == "ask_user"]
    assert len(ask_events) == 1
    qid = ask_events[0].payload["qid"]
    ok = await eng.ask_manager.resume("t4", qid, "能")
    assert ok is True
    done = await run_task
    assert done["final_text"] == "收到您的选择。"
    # 用户点选作为新 user 消息注入
    msgs = gw.chat_calls[1]["messages"]
    assert any(m["role"] == "user" and "能" in m["content"] for m in msgs)


async def test_react_max_rounds_error():
    reg, _ = build_registry()
    gw = MockGateway(script=[
        {"tool_calls": [("get_quote", {"symbol": f"60051{i}"})]} for i in range(MAX_ROUNDS)
    ] + [{"tool_calls": [("get_quote", {"symbol": "600519"})]}])
    eng = build_engine(gw, reg)
    stream = SessionStream("t5")
    done = await eng.run(ChatTask("t5", "循环"), stream)
    assert "error" in done and done["error"]
    errors = [e for e in stream._buffer if e.event == "error"]
    assert errors and errors[0].payload["code"] == "MAX_ROUNDS"
    assert errors[0].payload["recoverable"] is False


async def test_react_structured_report_submitted():
    """④ 结构化输出：submit_research_report 以 Function Calling 强约束提交。"""
    reg, _ = build_registry()
    report = {"meta": {"symbol": "600519", "name": "贵州茅台",
                       "data_as_of": "2026-11-27 10:00", "generated_at": "2026-11-27 10:05"},
              "sections": [{"type": "text", "text": "a [1]"}, {"type": "text", "text": "b [2] [3]"},
                           {"type": "text", "text": "c [4]"}],
              "citations": [{"n": 1, "tool": "get_quote", "ts": "2026-11-27", "excerpt": "100.0"},
                            {"n": 2, "tool": "get_kline", "ts": "2026-11-27", "excerpt": ""},
                            {"n": 3, "tool": "search_news", "ts": "2026-11-27", "excerpt": ""},
                            {"n": 4, "tool": "get_financial", "ts": "2026-11-27", "excerpt": ""}],
              "verdict": "neutral", "thesis_digest": "中性",
              "risk_disclosure": "本报告由教学演示智能体生成，仅供课程学习使用，"
                                 "不构成任何投资建议；数据可能存在延迟或误差，"
                                 "请以交易所官方信息为准。"}
    from backend.tools.research import ResearchReport

    async def h_report(meta, sections, citations, verdict="neutral",
                       thesis_digest="", risk_disclosure=""):
        r = ResearchReport(meta=meta, sections=sections, citations=citations,
                           verdict=verdict, thesis_digest=thesis_digest,
                           risk_disclosure=risk_disclosure)
        return {"ok": True, "report": r.model_dump()}

    reg.register(ToolSpec("submit_research_report", "submit", ResearchReport, h_report))
    gw = MockGateway(script=[
        {"tool_calls": [("submit_research_report", report)]},
    ])
    eng = build_engine(gw, reg)
    stream = SessionStream("t6")
    done = await eng.run(ChatTask("t6", "出报告"), stream)
    assert done["final_report"]["meta"]["symbol"] == "600519"


# ---------- 辩论 ----------

DP = "DATA_AS_OF: 2026-11-27\nDATA:\n[1] get_quote 现价 1500 [2] search_news 情绪偏多"


async def test_debate_two_rounds_flow():
    reg, calls = build_registry()
    gw = MockGateway(simple_texts={
        "你只依据 DataPack 中编号数据论证看多": json.dumps(
            [{"claim": "情绪利好", "cites": [2], "strength": "strong"}], ensure_ascii=False),
        "你只依据 DataPack 中编号数据论证看空": json.dumps(
            [{"claim": "涨幅已高", "cites": [1], "strength": "medium"}], ensure_ascii=False),
        "你是风控经理": json.dumps({"supported": [1, 2], "unsupported": [],
                                    "concerns": ["时效风险"]}, ensure_ascii=False),
    })
    gw.script = [
        {"text": DP},  # 分析师 DataPack（无工具调用，直接产出）
    ]
    orch = DebateOrchestrator(gw, reg, debate_rounds=2)
    stream = SessionStream("d1")
    out = await orch.run("d1", ["600519"], "600519 值得买吗？", stream)
    assert out["ok"] is True
    assert out["report"]["meta"]["symbol"] == "600519"
    assert out["debate"]["rounds"] == 2
    assert out["debate"]["risk_review"]["supported"] == [1, 2]
    assert out["datapack"].startswith("DATA_AS_OF")
    assert out["disclaimer"]


async def test_debate_one_round_fallback():
    """降级预案：辩论压缩为 1 轮（多空合并，role prompting 区分视角）。"""
    reg, calls = build_registry()
    gw = MockGateway(simple_texts={
        "你先以多方研究员身份论证看多": json.dumps(
            {"bull": ["看多论据 [1]"], "bear": ["看空论据 [2]"]}, ensure_ascii=False),
        "你是风控经理": json.dumps({"supported": [1], "unsupported": [2],
                                    "concerns": []}, ensure_ascii=False),
    })
    gw.script = [{"text": DP}]
    orch = DebateOrchestrator(gw, reg, debate_rounds=1)
    out = await orch.run("d2", ["600519"], "问题")
    assert out["debate"]["rounds"] == 1
    assert out["debate"]["risk_review"]["unsupported"] == [2]


async def test_debate_risk_json_parse_failure_safe():
    reg, _ = build_registry()
    gw = MockGateway(simple_texts={
        "你只依据 DataPack 中编号数据论证看多": "[]",
        "你只依据 DataPack 中编号数据论证看空": "[]",
        "你是风控经理": "这不是 JSON 输出",
    })
    gw.script = [{"text": DP}]
    orch = DebateOrchestrator(gw, reg, debate_rounds=2)
    out = await orch.run("d3", ["600519"], "问题")
    assert out["debate"]["risk_review"]["concerns"] == ["风控 JSON 解析失败"]


async def test_debate_unsupported_args_downweighted():
    """风控 unsupported 编号注入综合轮 prompt 强制降权。"""
    reg, _ = build_registry()
    gw = MockGateway(simple_texts={
        "你只依据 DataPack 中编号数据论证看多": "[]",
        "你只依据 DataPack 中编号数据论证看空": "[]",
        "你是风控经理": json.dumps({"supported": [1], "unsupported": [9],
                                    "concerns": ["无依据"]}, ensure_ascii=False),
    })
    gw.script = [{"text": DP}]
    orch = DebateOrchestrator(gw, reg, debate_rounds=1)
    out = await orch.run("d4", ["600519"], "问题")
    # 综合 prompt 中应出现 unsupported 编号（GW 最后一次 chat 调用的 system 消息）
    synth_call = gw.chat_calls[-1]
    system_msg = synth_call["messages"][0]["content"]
    assert "9" in system_msg and "无数据支撑" in system_msg
