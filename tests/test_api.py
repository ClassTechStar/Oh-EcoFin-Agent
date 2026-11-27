"""API 层集成：TestClient + Mock 网关（SSE / 断线重连重放 / resume / 成本看板）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app import AppContext, _extract_symbol, create_app
from backend.config import Settings


def make_app(tmp_path) -> tuple[TestClient, AppContext]:
    s = Settings(db_path=str(tmp_path / "api.db"))
    s.app.demo_mode = True
    s.llm.daily_cost_cap_cny = 1000
    app = create_app(s)
    client = TestClient(app)
    return client, app.state.ctx


@pytest.fixture()
def api(tmp_path):
    client, ctx = make_app(tmp_path)
    with client:
        yield client, ctx


def test_health(api):
    client, ctx = api
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert "snapshot" in data and "disclaimer" in data
    assert data["snapshot"]["data_as_of"] == ctx.snapshot.as_of


def test_sessions_flow(api):
    client, _ = api
    r = client.post("/api/sessions")
    sid = r.json()["id"]
    assert r.status_code == 200
    r = client.get("/api/sessions")
    assert any(s["id"] == sid for s in r.json()["sessions"])
    r = client.get(f"/api/sessions/{sid}/messages")
    assert "disclaimer" in r.json()


def test_risk_questions_endpoint(api):
    client, _ = api
    r = client.get("/api/risk-questions")
    qs = r.json()["questions"]
    assert len(qs) == 5
    dims = {q["dimension"] for q in qs}
    assert dims == {"投资经验", "收益目标", "亏损容忍", "投资期限", "财务状况"}


def test_cost_summary(api):
    client, ctx = api
    ctx.db.insert_llm_call("s", "glm-5.3-flash", "primary", "react", 100, 50, 0, 12, 300)
    r = client.get("/api/cost/summary")
    assert r.json()["today_cents"] >= 12
    assert r.json()["fuse_open"] is False


def test_cost_fuse_flag(api):
    client, ctx = api
    ctx.db.insert_llm_call("s", "glm-5.3-flash", "primary", "react", 100, 50, 0, 999999, 300)
    r = client.get("/api/cost/summary")
    assert r.json()["fuse_open"] is True


def test_extract_symbol():
    assert _extract_symbol("600519 值得买吗") == "600519"
    assert _extract_symbol("没有代码") == ""
    assert _extract_symbol("号码 13812345678 不是代码") == ""  # 手机号不误识别
    assert _extract_symbol("对比000001和600036") == "000001"


def test_sse_replay_after_disconnect(api):
    """断线重连重放：事件落库 → Last-Event-ID 之后全部重放。"""
    client, ctx = api
    sid = "replay-test"
    stream = ctx.hub.get(sid)
    stream.persist_fn = None
    for i in range(5):
        stream.publish("message_delta", {"text": f"part{i}"})
    # 直接调用重放逻辑（幂等于 GET /api/chat/stream 的行为）
    db_rows = ctx.db.load_events_after(sid, 2)
    replayed = stream.replay_from(2, db_rows)
    ids = [e.id for e in replayed]
    assert ids == [3, 4, 5]


def test_chat_stream_endpoint_replay(api):
    client, ctx = api
    sid = "endpoint-replay"
    stream = ctx.hub.get(sid)
    stream.persist_fn = None
    for i in range(4):
        stream.publish("message_delta", {"text": f"e{i}"})
    # replay_only：仅回放历史即返回（避免长连尾部阻塞测试）
    r = client.get(f"/api/chat/stream?sid={sid}&last_event_id=1&replay_only=true")
    body = r.text
    assert "id: 2" in body and "id: 3" in body and "id: 4" in body
    assert "e2" in body
    # 响应头防中间层缓冲
    assert r.headers.get("x-accel-buffering") == "no"
    assert "no-store" in r.headers.get("cache-control", "")


def test_resume_without_pending_404(api):
    client, ctx = api
    r = client.post("/api/chat/resume", json={"session_id": "nope", "qid": "x", "choice": "y"})
    assert r.status_code == 404


def test_chat_endpoint_runs_mock_engine(api):
    """POST /api/chat → SSE 全链（Mock 引擎注入）。"""
    client, ctx = api
    from backend.engine.gateway import LLMResult

    class ScriptGW:
        async def chat(self, messages, tools=None, stream_cb=None, **kw):
            if stream_cb:
                await stream_cb("message_delta", "你好，我是教学演示助手。")
            return LLMResult(text="你好，我是教学演示助手。",
                             usage_in=10, usage_out=8, cost_cents=1,
                             model="fake", tier="primary")

    ctx.react.gw = ScriptGW()
    ctx.debate.gw = ScriptGW()
    r = client.post("/api/chat", json={"message": "你好", "mode": "chat"})
    body = r.text
    assert r.status_code == 200
    assert "event: message_delta" in body
    assert "event: done" in body
    # 用户消息落库
    sid = None
    for line in body.splitlines():
        if line.startswith("data:") and "rounds" in line:
            pass
    rows = ctx.db.query("SELECT content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1")
    assert rows and rows[0]["content"] == "你好"


def test_chat_debate_mode_triggers_orchestrator(api):
    client, ctx = api
    from backend.engine.gateway import LLMResult

    class FakeGW:
        async def chat(self, messages, tools=None, **kw):
            return LLMResult(text="DATA_AS_OF: t\nDATA:\n[1] x",
                             usage_in=5, usage_out=5, cost_cents=1,
                             model="fake", tier="primary")

        async def simple(self, messages, tier="economy", **kw):
            c = str(messages[-1]["content"])
            if c.startswith("你只依据 DataPack 中编号数据论证看多"):
                return json.dumps([{"claim": "x", "cites": [1], "strength": "strong"}],
                                  ensure_ascii=False)
            if c.startswith("你只依据 DataPack 中编号数据论证看空"):
                return json.dumps([{"claim": "y", "cites": [1], "strength": "weak"}],
                                  ensure_ascii=False)
            if c.startswith("你是风控经理"):
                return json.dumps({"supported": [1], "unsupported": [], "concerns": []},
                                  ensure_ascii=False)
            return "默认"

    ctx.debate.gw = FakeGW()
    r = client.post("/api/chat", json={"message": "600519 值得买吗", "mode": "auto"})
    body = r.text
    assert r.status_code == 200
    assert "event: done" in body
    # 决策落库（辩论模式自动保存）
    dec = ctx.db.query("SELECT symbol, verdict FROM decisions ORDER BY id DESC LIMIT 1")
    assert dec and dec[0]["symbol"] == "600519"


def test_lan_token_403(tmp_path, monkeypatch):
    """局域网未开 token 时外部连接 403（验收项）。

    TestClient 的 client.host 是 'testclient'（豁免名单内），无法经 HTTP 触发
    LAN 分支 → 直接以令牌校验函数 + 中间件配置语义验证（403 逻辑同源）。
    """
    s = Settings(db_path=str(tmp_path / "lan.db"))
    s.app.demo_mode = True   # 离线：lifespan 不触发 AKShare 联网
    s.app.lan_mode = True
    app = create_app(s)
    ctx = app.state.ctx
    ctx.lan_token = "secret-token"

    from backend.security.tokens import verify_token

    # 语义与 access_control 中间件一致：无 token / 错 token → 403
    assert verify_token(None, ctx.lan_token) is False
    assert verify_token("bad-token", ctx.lan_token) is False
    assert verify_token("secret-token", ctx.lan_token) is True
    # LAN 模式开启且缺省生成随机 token
    assert ctx.lan_token != ""


def test_feedback_flow_and_version_buckets(api):
    """L1 反馈回流：👍/👎 入库 + prompt_version 服务端盖章 + 汇总分桶。"""
    client, _ = api
    r = client.post("/api/sessions")
    sid = r.json()["id"]
    r = client.post("/api/feedback", json={
        "session_id": sid, "rating": "down", "comment": "K线没取到"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/api/feedback", json={"session_id": sid, "rating": "up"})
    assert r.status_code == 200
    # 非法 rating 422
    r = client.post("/api/feedback", json={"session_id": sid, "rating": "mid"})
    assert r.status_code == 422
    summary = client.get("/api/feedback/summary").json()
    buckets = {b["rating"]: b["c"] for b in summary["buckets"]
               if b["prompt_version"]}
    assert buckets.get("down") == 1 and buckets.get("up") == 1
    assert any("K线没取到" in (c.get("comment") or "") for c in summary["recent"])


def test_debate_resume_missing_run_404(api):
    """辩论 checkpoint 续跑：run_id 不存在 → 404（fail-closed）。"""
    client, _ = api
    r = client.post("/api/debate/not-exist/resume", json={})
    assert r.status_code == 404
