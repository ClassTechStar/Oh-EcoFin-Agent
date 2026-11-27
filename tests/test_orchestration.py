"""M6 Agent 编排：状态机、单一终态、副作用确认、工具失败、上下文预算。"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field

from backend.engine.askuser import AskUserManager
from backend.engine.compactor import compact_analysis, compact_report, compact_sql
from backend.engine.context import fit_context
from backend.engine.events import SessionStream
from backend.engine.gateway import LLMResult, RecoverableLLMError, ToolCall
from backend.engine.react import ChatTask, ReActEngine
from backend.engine.run_state import RunBudget
from backend.engine.runs import RunStore
from backend.engine.tool_calls import ToolCallStore
from backend.tools.registry import ToolRegistry, ToolSpec


class QuoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(..., min_length=1)


class OrderParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: str = Field(..., min_length=1)
    qty: int = Field(..., ge=1)


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MockGateway:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.chat_calls: list[dict] = []

    async def chat(self, messages, tools=None, stream_cb=None, **kwargs):
        self.chat_calls.append({"messages": [dict(m) for m in messages],
                                "tools": tools})
        step = self.script.pop(0) if self.script else {"text": "done"}
        if isinstance(step, Exception):
            raise step
        text = step.get("text", "")
        if text and stream_cb:
            await stream_cb("message_delta", text)
        calls = [
            ToolCall(id=f"c{i}", name=name, arguments=json.dumps(args,
                                                               ensure_ascii=False))
            for i, (name, args) in enumerate(step.get("tool_calls", []))
        ]
        return LLMResult(text=text, tool_calls=calls, usage_in=10, usage_out=5,
                         cost_cents=1, model="fake", tier="primary")


def _engine(gateway, registry, tmp_db=None, budget=None):
    db = tmp_db
    if db is None:
        import tempfile

        from backend.db import Database
        db = Database(tempfile.mktemp(suffix=".db"))
    return ReActEngine(gateway, registry, AskUserManager(db, timeout_seconds=5),
                       budget=budget, run_store=RunStore(db),
                       tool_store=ToolCallStore(db))


def _registry():
    reg = ToolRegistry()
    calls: list[tuple] = []

    async def quote(symbol):
        return {"ok": True, "symbol": symbol, "price": 100.0}

    async def fail():
        raise RuntimeError("boom")

    async def order(side, qty):
        calls.append((side, qty))
        return {"ok": True, "side": side, "qty": qty}

    async def big():
        return {"ok": True, "table": {
            "columns": ["i", "text"],
            "rows": [[i, "x" * 50] for i in range(500)],
        }}

    reg.register(ToolSpec("quote", "q", QuoteParams, quote, parallel_safe=True))
    reg.register(ToolSpec("fail", "f", NoParams, fail, parallel_safe=False))
    reg.register(ToolSpec("submit_order", "o", OrderParams, order,
                          parallel_safe=False, side_effect="external",
                          confirmation="explicit_request"))
    reg.register(ToolSpec("big", "b", NoParams, big, parallel_safe=True))
    return reg, calls


def _events(stream, name):
    return [event for event in stream._buffer if event.event == name]


def test_single_terminal_and_state_sequence(tmp_db):
    reg, _ = _registry()
    gw = MockGateway([
        {"tool_calls": [("quote", {"symbol": "600519"})]},
        {"text": "完成了"},
    ])
    stream = SessionStream("s1")
    eng = _engine(gw, reg, tmp_db)
    done = asyncio.run(eng.run(ChatTask("s1", "看看 600519"), stream))

    assert done["terminal"] is True
    assert done["stop_reason"] == "answer"
    assert done["state"] == "done"
    assert done["final_text"] == "完成了"
    assert len(_events(stream, "done")) == 1
    states = [event.payload["state"] for event in _events(stream, "run_state")]
    assert states[0] == "planning"
    assert "acting" in states and "observing" in states
    assert states[-1] == "done"

    row = RunStore(tmp_db).get(done["run_id"])
    assert row["state"] == "done"
    assert row["rounds"] == 2
    assert row["tool_calls"] == 1

    raw = ToolCallStore(tmp_db).recent("s1")
    assert raw and raw[0]["name"] == "quote"
    assert '"price": 100.0' in raw[0]["result_json"]


def test_llm_error_has_error_and_one_done(tmp_db):
    reg, _ = _registry()
    gw = MockGateway([RecoverableLLMError("模型暂不可用")])
    stream = SessionStream("s2")
    done = asyncio.run(_engine(gw, reg, tmp_db).run(ChatTask("s2", "问"), stream))
    assert done["stop_reason"] == "llm_unavailable"
    assert done["state"] == "failed"
    assert len(_events(stream, "error")) == 1
    assert _events(stream, "error")[0].payload["recoverable"] is True
    assert len(_events(stream, "done")) == 1


def test_max_rounds_has_error_and_one_done(tmp_db):
    reg, _ = _registry()
    max_rounds = 2
    budget = RunBudget(max_rounds=max_rounds)
    gw = MockGateway([
        {"tool_calls": [("quote", {"symbol": "600519"})]}
        for _ in range(max_rounds)
    ])
    stream = SessionStream("s3")
    done = asyncio.run(_engine(gw, reg, tmp_db, budget).run(
        ChatTask("s3", "循环"), stream))
    assert done["stop_reason"] == "max_rounds"
    assert done["state"] == "failed"
    assert _events(stream, "error")[0].payload["code"] == "MAX_ROUNDS"
    assert len(_events(stream, "done")) == 1


def test_tool_failure_is_visible(tmp_db):
    reg, _ = _registry()
    gw = MockGateway([
        {"tool_calls": [("fail", {})]},
        {"text": "工具失败后继续"},
    ])
    stream = SessionStream("s4")
    done = asyncio.run(_engine(gw, reg, tmp_db).run(
        ChatTask("s4", "测试失败"), stream))
    assert done["tool_stats"]["errors"] == 1
    tool_events = [e.payload for e in _events(stream, "tool_call")
                   if e.payload.get("status") == "error"]
    assert tool_events and tool_events[0]["error"]
    assert len(_events(stream, "done")) == 1


def test_side_effect_confirmation_blocks_then_allows(tmp_db):
    reg, calls = _registry()
    stream = SessionStream("s5")
    gw = MockGateway([
        {"tool_calls": [("submit_order", {"side": "buy", "qty": 100})]},
        {"text": "请确认是否下单"},
    ])
    done = asyncio.run(_engine(gw, reg, tmp_db).run(
        ChatTask("s5", "600519 值得买吗"), stream))
    assert calls == []
    assert done["tool_stats"]["blocked"] == 1
    blocked = [e.payload for e in _events(stream, "tool_call")
               if e.payload.get("requires_confirmation")]
    assert blocked and blocked[0]["side_effect"] == "external"
    assert any(e.payload.get("code") == "CONFIRMATION_REQUIRED"
               for e in _events(stream, "error"))

    gw2 = MockGateway([
        {"tool_calls": [("submit_order", {"side": "buy", "qty": 100})]},
        {"text": "已提交"},
    ])
    stream2 = SessionStream("s6")
    done2 = asyncio.run(_engine(gw2, reg, tmp_db).run(
        ChatTask("s6", "帮我买入 100 股 600519"), stream2))
    assert calls == [("buy", 100)]
    assert done2["tool_stats"]["blocked"] == 0
    assert done2["stop_reason"] == "answer"


def test_context_budget_wrap_up(tmp_db):
    reg, _ = _registry()
    budget = RunBudget(max_rounds=3, context_tokens=5,
                       tool_result_tokens=50, keep_recent_messages=2)
    gw = MockGateway([{"text": "不应调用到"}])
    stream = SessionStream("s7")
    done = asyncio.run(_engine(gw, reg, tmp_db, budget).run(
        ChatTask("s7", "问"), stream))
    assert gw.chat_calls == []
    assert done["stop_reason"] == "context_budget"
    assert done["state"] == "done"
    assert done["truncated"] is True
    assert done["final_text"]
    assert len(_events(stream, "done")) == 1
    assert _events(stream, "budget")[0].payload["action"] == "wrap_up"


def test_fit_context_compresses_and_keeps_tool_pairing():
    huge = json.dumps({"ok": True, "table": {
        "columns": ["i"], "rows": [[("x" * 100)] for _ in range(200)]}})
    messages = [
        {"role": "system", "content": "系统提示" * 300},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "big", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": huge},
        {"role": "user", "content": "最新问题"},
        {"role": "assistant", "content": "阶段结论"},
    ]
    fit = fit_context(messages, context_tokens=1200,
                      tool_result_tokens=300, keep_recent_messages=3)
    assert fit.compressed is True
    assert fit.used_after <= fit.used_before
    assert any("budget_note" in str(m.get("content")) or "截断" in str(m.get("content"))
               for m in fit.messages if m.get("role") == "tool")

    assistant_ids = {
        call["id"] for m in fit.messages if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    tool_ids = {m.get("tool_call_id") for m in fit.messages
                if m.get("role") == "tool"}
    assert assistant_ids <= tool_ids or not assistant_ids


def test_new_tool_compactors_are_bounded():
    sql = {"ok": True, "columns": ["a"], "rows": [[i] for i in range(100)],
           "row_count": 100, "sql_hash": "s", "result_hash": "r"}
    assert len(compact_sql(sql)["rows"]) == 20
    analysis = {
        "ok": True, "module": "trend", "result_hash": "r",
        "chart": {"chart_type": "line", "title": "t", "spec_hash": "c",
                  "option": {"series": [{"data": list(range(500))}]}},
        "data": {"table": {"columns": ["x"], "rows": [[i] for i in range(500)]},
                 "x": list(range(500))},
    }
    compacted = compact_analysis(analysis)
    assert len(compacted["table"]["rows"]) == 20
    assert "option" not in compacted["chart"]
    report = {"report_id": "r", "title": "t", "spec_hash": "s",
              "files": [], "sources": [], "blocks": [
                  {"id": "b1", "type": "analysis", "title": "x",
                   "data": {"table": {"rows": [[i] for i in range(100)]}}}]}
    assert "data" not in compact_report(report)["blocks"][0]


def test_registry_confirmation_metadata():
    reg, calls = _registry()
    blocked = asyncio.run(reg.execute(
        "submit_order", json.dumps({"side": "buy", "qty": 1}),
        allow_side_effects=False))
    assert blocked["ok"] is False
    assert blocked["requires_confirmation"] is True
    assert calls == []
    allowed = asyncio.run(reg.execute(
        "submit_order", json.dumps({"side": "buy", "qty": 1}),
        allow_side_effects=True))
    assert allowed["ok"] is True
    assert calls == [("buy", 1)]
    assert reg.side_effect_map()["submit_order"]["side_effect"] == "external"
