"""委员会协议（M6）：不可变上下文 / 六段原文 / 失败可见 / 落库回放。"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from backend.db import Database
from backend.engine.committee import (
    CommitteeTranscript,
    RoleTurn,
    build_context,
    load_audit,
    persist_audit,
)
from backend.engine.debate import DebateOrchestrator
from backend.engine.gateway import LLMResult
from backend.tools.registry import ToolRegistry, ToolSpec

DP = "DATA_AS_OF: 2026-11-27\nDATA:\n[1] get_quote 现价 1500 [2] search_news 情绪偏多"


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(..., description="s")


class StubGateway:
    """最小网关：chat 走剧本，simple 按前缀回放；可注入指定角色的上游故障。

    风控复核升级为 submit_risk_review 工具化提交：tools 含该工具时直接回
    Function Calling（Schema 强约束路径）；工具缺失时回退文本（正则兜底路径）。
    """

    def __init__(self, texts: dict[str, str], fail_key: str = "",
                 risk_via_tool: bool = True):
        self.texts = texts
        self.fail_key = fail_key
        self.risk_via_tool = risk_via_tool
        self.script = [DP]
        self.chat_calls: list = []

    async def chat(self, messages, tools=None, tier="primary", *, session_id="",
                   role="chat", stream_cb=None, temperature=0.3):
        self.chat_calls.append({"messages": [dict(m) for m in messages],
                                "tools": tools or []})
        if role == "debate:risk":
            if self.risk_via_tool:
                from backend.engine.gateway import ToolCall

                args = json.dumps({"supported": [1, 2], "unsupported": [],
                                   "concerns": []}, ensure_ascii=False)
                return LLMResult(text="", tool_calls=[ToolCall(id="risk1",
                                                               name="submit_risk_review",
                                                               arguments=args)],
                                 usage_in=10, usage_out=5, cost_cents=0,
                                 model="stub", tier=tier)
            # 正则兜底路径：直接回放风控文本（不消耗分析师剧本）
            text = self.texts.get("你是风控经理", "剧本耗尽")
            return LLMResult(text=text, tool_calls=[], usage_in=10, usage_out=5,
                             cost_cents=0, model="stub", tier=tier)
        text = self.script.pop(0) if self.script else "剧本耗尽"
        return LLMResult(text=text, tool_calls=[], usage_in=10, usage_out=5,
                         cost_cents=0, model="stub", tier=tier)

    async def simple(self, messages, tier="economy", **kw):
        content = str(messages[-1]["content"])
        if self.fail_key and self.fail_key in content:
            raise RuntimeError("upstream 502")
        for key, value in self.texts.items():
            if content.startswith(key):
                return value
        return "默认"


def _registry(risk_tool: bool = True) -> ToolRegistry:
    reg = ToolRegistry()

    async def h_quote(symbol):
        return {"ok": True, "symbol": symbol, "price": 100.0}

    reg.register(ToolSpec("get_quote", "q", _P, h_quote, parallel_safe=True))

    if risk_tool:
        from backend.tools.research import RiskReview

        async def h_risk(supported=None, unsupported=None, concerns=None, **_):
            review = RiskReview(supported=supported or [],
                                unsupported=unsupported or [],
                                concerns=concerns or [])
            from backend.tools.research import risk_review_payload
            return {"ok": True, "risk_review": risk_review_payload(review)}

        reg.register(ToolSpec("submit_risk_review", "r", RiskReview, h_risk,
                              parallel_safe=False))
    return reg


TEXTS = {
    "你只依据 DataPack 中编号数据论证看多": json.dumps(
        [{"claim": "情绪利好", "cites": [2]}], ensure_ascii=False),
    "你只依据 DataPack 中编号数据论证看空": json.dumps(
        [{"claim": "涨幅已高", "cites": [1]}], ensure_ascii=False),
    "你是风控经理": json.dumps({"supported": [1, 2], "unsupported": [],
                                "concerns": []}, ensure_ascii=False),
}


# ---------- 上下文快照 ----------

def test_context_id_deterministic_and_sensitive():
    facts = [{"tool": "get_quote", "result": {"price": 1500}}]
    a = build_context(["600519"], facts, "2026-11-27", "值得买吗")
    b = build_context(["600519"], facts, "2026-11-27", "值得买吗")
    assert a.context_id == b.context_id
    assert a.context_id.startswith("ctx_") and len(a.context_id) == 20
    # 同一事实集换个时点 → 必须换 context_id（禁止静默复用旧答案）
    c = build_context(["600519"], facts, "2026-11-28", "值得买吗")
    assert c.context_id != a.context_id
    audit = a.to_audit_dict()
    assert audit["payload_sha256"] and audit["fact_count"] == 1


# ---------- 六段原文 ----------

def test_missing_segment_is_none_not_fake_neutral():
    ctx = build_context(["600519"], [], "2026-11-27", "q")
    tr = CommitteeTranscript(context=ctx)
    tr.ok_turn("analyst", 0, "证据准备")
    tr.failed_turn("bull", 1, "模型超时")
    tr.ok_turn("bear", 1, "看空论据")
    tr.verification_required = True
    seg = tr.segments()
    assert seg["analyst"] == "证据准备"
    assert seg["bull_r1"] is None          # 失败段不伪造
    assert seg["bull_r2"] is None          # 没跑就是没跑
    assert seg["bear_r1"] == "看空论据"
    assert tr.validate() == []


def test_failed_turn_with_text_or_signal_flagged():
    ctx = build_context(["600519"], [], "2026-11-27", "q")
    tr = CommitteeTranscript(context=ctx, verification_required=True)
    tr.add(RoleTurn(role="bull", round=1, status="failed", text="中性",
                    error="timeout", signal=0.0, context_id=ctx.context_id))
    issues = tr.validate()
    assert any("疑似伪造意见" in i for i in issues)
    assert any("疑似伪造" in i and "signal" in i for i in issues)


def test_revision_requires_change_reason():
    ctx = build_context(["600519"], [], "2026-11-27", "q")
    tr = CommitteeTranscript(context=ctx)
    tr.add(RoleTurn(role="bull", round=1, text="看多", signal=0.6,
                    context_id=ctx.context_id))
    tr.add(RoleTurn(role="bull", round=2, text="改判看空", signal=-0.3,
                    context_id=ctx.context_id))
    assert any("未说明理由" in i for i in tr.validate())

    ok = CommitteeTranscript(context=ctx)
    ok.add(RoleTurn(role="bull", round=1, text="看多", signal=0.6,
                    context_id=ctx.context_id))
    ok.add(RoleTurn(role="bull", round=2, text="改判看空", signal=-0.3,
                    context_id=ctx.context_id, change_reason="新证据：交易所问询函"))
    assert ok.validate() == []


def test_context_mismatch_flagged():
    ctx = build_context(["600519"], [], "2026-11-27", "q")
    tr = CommitteeTranscript(context=ctx)
    tr.add(RoleTurn(role="bull", round=1, text="看多", context_id="ctx_other"))
    assert any("context_id" in i for i in tr.validate())


# ---------- 编排器集成 ----------

async def test_orchestrator_transcript_consistent():
    gw = StubGateway(dict(TEXTS))
    orch = DebateOrchestrator(gw, _registry(), debate_rounds=2)
    out = await orch.run("c1", ["600519"], "600519 值得买吗？")
    tr = out["transcript"]
    assert tr["context"]["context_id"] == out["context"]["context_id"]
    assert tr["segments"]["analyst"] == DP
    for key in ("bull_r1", "bear_r1", "bull_r2", "bear_r2", "cio"):
        assert tr["segments"][key]
    assert tr["issues"] == []
    assert out["verification_required"] is False
    assert all(t["context_id"] == out["context"]["context_id"] for t in tr["turns"])


async def test_orchestrator_role_failure_visible():
    gw = StubGateway(dict(TEXTS), fail_key="你只依据 DataPack 中编号数据论证看多")
    orch = DebateOrchestrator(gw, _registry(), debate_rounds=2)
    out = await orch.run("c2", ["600519"], "问题")
    assert out["verification_required"] is True
    assert out["transcript"]["segments"]["bull_r1"] is None
    failures = out["debate"]["failures"]
    assert any(f["role"] == "bull" and f["round"] == 1 for f in failures)
    assert all(f["text"] == "" for f in failures)  # 失败不伪造意见
    assert any("bull r1" in n for n in out["transcript"]["verification_notes"])
    assert out["report"] and out["report"]["verdict"]


# ---------- 落库与回放 ----------

def test_persist_and_load_audit_roundtrip(tmp_path):
    ctx = build_context(["600519"], [{"tool": "get_quote", "result": {"price": 1500}}],
                        "2026-11-27 10:00", "值得买吗")
    tr = CommitteeTranscript(context=ctx)
    tr.ok_turn("analyst", 0, "证据准备" * 50)  # 长文本不允许被截断
    tr.ok_turn("bull", 1, "看多")
    tr.ok_turn("bear", 1, "看空")
    tr.verification_required = True
    audit = tr.to_audit_dict()

    db = Database(tmp_path / "committee.db")
    run_id = persist_audit(db, audit, symbol="600519", verdict="bullish",
                           status="review", run_id="run-1")
    loaded = load_audit(db, run_id)
    assert loaded is not None
    assert loaded["run"]["context_id"] == ctx.context_id
    assert loaded["run"]["verdict"] == "bullish"
    roles = {(s["role"], s["round"]) for s in loaded["sections"]}
    assert ("analyst", 0) in roles and ("bull", 1) in roles
    analyst = next(s for s in loaded["sections"] if s["role"] == "analyst")
    assert analyst["raw_text"] == "证据准备" * 50
    assert load_audit(db, "missing") is None


async def test_risk_review_regex_fallback_when_no_tool_call():
    """模型未走 submit_risk_review 工具 → 正则提 JSON 兜底（解析成功不抬升复核）。"""
    gw = StubGateway(dict(TEXTS), risk_via_tool=False)
    orch = DebateOrchestrator(gw, _registry(), debate_rounds=2)
    out = await orch.run("c3", ["600519"], "600519 值得买吗？")
    risk = out["debate"]["risk_review"]
    assert risk["supported"] == [1, 2] and risk["concerns"] == []
    assert out["verification_required"] is False


async def test_risk_review_parse_failure_flags_verification():
    """风控 JSON 彻底解析失败 → 必须抬升 verification_required（诚实降级）。"""
    gw = StubGateway({}, risk_via_tool=False)
    orch = DebateOrchestrator(gw, _registry(risk_tool=False), debate_rounds=1)
    out = await orch.run("c4", ["600519"], "问题")
    assert out["debate"]["risk_review"]["concerns"]
    assert any("解析失败" in c for c in out["debate"]["risk_review"]["concerns"])
    assert out["verification_required"] is True
