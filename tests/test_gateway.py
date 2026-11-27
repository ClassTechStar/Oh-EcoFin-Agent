"""LLMGateway：成本估算 / 工具参数容错解析 / 日成本熔断。"""

from __future__ import annotations

import json

import pytest

from backend.engine.gateway import (
    CostFuseOpenError,
    CostLedger,
    LLMGateway,
    estimate_cost_cents,
    parse_tool_calls,
)


class FakeADB:
    def __init__(self, spent=0):
        self.spent = spent

    async def cost_today_cents(self):
        return self.spent

    async def insert_llm_call(self, **kw):
        self.spent += kw.get("cost_cents", 0)


def test_estimate_cost_cached_discount():
    plain = estimate_cost_cents("glm-5.3-flash", 1_000_000, 0, 0)
    cached = estimate_cost_cents("glm-5.3-flash", 1_000_000, 0, 500_000)
    assert cached < plain
    out_cost = estimate_cost_cents("glm-5.3-flash", 0, 1_000_000, 0)
    assert out_cost > plain / 2  # 输出价高于输入


def test_estimate_deepseek_cache_near_zero():
    c = estimate_cost_cents("deepseek-flash", 0, 0, 1_000_000)
    assert c == 2  # 缓存命中 0.02 元/M = 2 分，近乎免费


def test_parse_tool_calls_arguments_is_json_string():
    raw = [{"id": "a", "function": {"name": "get_quote",
                                    "arguments": '{"symbols": ["600519"]}'}}]
    calls = parse_tool_calls(raw)
    assert calls[0].name == "get_quote"
    assert json.loads(calls[0].arguments)["symbols"] == ["600519"]


def test_parse_tool_calls_tolerates_whitespace_and_invalid():
    raw = [{"id": "a", "function": {"name": "x", "arguments": '  {}  '}},
           {"id": "b", "function": {"name": "y", "arguments": '{bad json'}}]
    calls = parse_tool_calls(raw)
    assert calls[0].arguments.strip() == "{}"
    # 非法 JSON 保留原文，交沙箱拒绝路径（不在此处抛异常）
    assert calls[1].arguments == "{bad json"


def test_cost_ledger_fuse_opens(tmp_db):
    led = CostLedger(None, cap_cny=0.05)  # 5 分
    led.adb = FakeADB(spent=10)
    import asyncio

    with pytest.raises(CostFuseOpenError):
        asyncio.run(led.guard("primary"))


def test_cost_ledger_under_cap_passes(tmp_db):
    led = CostLedger(None, cap_cny=20.0)
    led.adb = FakeADB(spent=100)
    import asyncio

    asyncio.run(led.guard("primary"))  # 不抛


def test_gateway_tier_routing(tmp_db, monkeypatch):
    """脚本验证 tier 路由与降级链（无网络：替换内部 _call）。"""
    settings = type("S", (), {"llm": type("L", (), {
        "primary_model": "glm", "economy_model": "ds",
        "primary_base_url": "u1", "economy_base_url": "u2",
        "timeout_seconds": 5})()})()
    led = CostLedger(None, 100)
    led.adb = FakeADB(0)
    gw = LLMGateway(settings, led, "k1", "k2")
    calls = []

    async def fake_call(tier, *a, **kw):
        calls.append(tier)
        if tier == "primary":
            raise TimeoutError()
        return "ok"

    monkeypatch.setattr(gw, "_call", fake_call)
    import asyncio

    out = asyncio.run(gw.chat([{"role": "user", "content": "hi"}]))
    assert out == "ok"
    assert calls == ["primary", "economy"]  # 降级链：仅 primary 降级


def test_gateway_economy_no_fallback(tmp_db, monkeypatch):
    settings = type("S", (), {"llm": type("L", (), {
        "primary_model": "glm", "economy_model": "ds",
        "primary_base_url": "u1", "economy_base_url": "u2",
        "timeout_seconds": 5})()})()
    led = CostLedger(None, 100)
    led.adb = FakeADB(0)
    gw = LLMGateway(settings, led, "k1", "k2")

    async def fake_call(tier, *a, **kw):
        raise TimeoutError()

    monkeypatch.setattr(gw, "_call", fake_call)
    import asyncio

    with pytest.raises(Exception):
        asyncio.run(gw.chat([{"role": "user", "content": "hi"}], tier="economy"))
