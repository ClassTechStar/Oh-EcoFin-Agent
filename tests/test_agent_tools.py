"""Agent 工具面：信号 / 仿真账户 / 仿真下单（沙箱参数 + 风控不可绕过）。"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.app import AppContext, create_app
from backend.config import Settings
from backend.quant.fixtures import synthetic_ohlcv

TRADING_TOOLS = {"get_alpha_signals", "get_paper_account", "submit_paper_order"}


def _uptrend(days: int = 240) -> pd.DataFrame:
    df = synthetic_ohlcv(days=days, seed=11)
    closes = [100.0 * (1.001 ** i) for i in range(len(df))]
    df["close"] = closes
    df["open"] = [c * 0.999 for c in closes]
    df["high"] = [c * 1.004 for c in closes]
    df["low"] = [c * 0.996 for c in closes]
    return df


def make_app(tmp_path) -> tuple[TestClient, AppContext]:
    s = Settings(db_path=str(tmp_path / "agent_tools.db"))
    s.app.demo_mode = True
    s.llm.daily_cost_cap_cny = 1000
    app = create_app(s)
    return TestClient(app), app.state.ctx


@pytest.fixture()
def api(tmp_path):
    client, ctx = make_app(tmp_path)
    with client:
        ctx.snapshot._swap(pd.DataFrame([{
            "代码": "600519", "名称": "贵州茅台", "最新价": 100.0, "涨跌幅": 2.0,
            "成交量": 1_000_000, "成交额": 1e8, "换手率": 1.2, "量比": 1.1,
            "总市值": 2e12, "市盈率-动态": 30.0, "市净率": 8.0,
        }]), "test")
        trend = _uptrend()

        async def fake_kline(symbol: str, days: int = 120, adjust: str = "hfq"):
            return trend

        ctx.market.get_kline_raw = fake_kline  # type: ignore[assignment]
        yield client, ctx


def test_registry_exposes_trading_tools(api):
    _, ctx = api
    assert TRADING_TOOLS.issubset(set(ctx.registry.names()))
    schema_names = {s["function"]["name"] for s in ctx.registry.schemas()}
    assert TRADING_TOOLS.issubset(schema_names)
    # 交易类工具必须串行（防并发下单）
    assert "submit_paper_order" not in ctx.registry.parallel_safe_names()


async def test_get_alpha_signals_tool(api):
    _, ctx = api
    out = await ctx.registry.execute("get_alpha_signals", json.dumps({"symbol": "600519"}))
    assert out["ok"] is True, out
    assert out["symbol"] == "600519"
    assert len(out["signals"]) == 3
    assert out["weights"]["600519"] > 0
    assert out["weights"]["600519"] <= 0.25 + 1e-9
    assert "flat_reason" in out


async def test_paper_account_and_order_tools(api):
    _, ctx = api
    account = await ctx.registry.execute("get_paper_account", json.dumps({"risk_level": "R3"}))
    assert account["ok"] is True
    assert account["cash"] == 1_000_000

    order = await ctx.registry.execute("submit_paper_order", json.dumps({
        "symbol": "600519", "side": "buy", "qty": 100, "risk_level": "R3"}))
    assert order["ok"] is True, order
    assert order["fill"]["qty"] == 100
    assert order["account"]["positions"][0]["available_qty"] == 0

    account = await ctx.registry.execute("get_paper_account", json.dumps({"risk_level": "R3"}))
    assert account["positions"][0]["qty"] == 100
    assert account["reconcile"]["ok"] is True


async def test_order_tool_blocked_by_risk(api):
    _, ctx = api
    out = await ctx.registry.execute("submit_paper_order", json.dumps({
        "symbol": "600519", "side": "buy", "qty": 100, "risk_level": "R1"}))
    assert out["ok"] is False
    assert "R1" in out["reason"]
    assert ctx.broker.positions == {}


async def test_tool_params_are_sandboxed(api):
    _, ctx = api
    bad_side = await ctx.registry.execute("submit_paper_order", json.dumps({
        "symbol": "600519", "side": "hold", "qty": 100}))
    assert bad_side["ok"] is False and "参数校验失败" in bad_side["error"]

    bad_symbol = await ctx.registry.execute("get_alpha_signals", json.dumps({"symbol": "AAPL"}))
    assert bad_symbol["ok"] is False

    bad_risk = await ctx.registry.execute("get_paper_account", json.dumps({"risk_level": "R9"}))
    assert bad_risk["ok"] is False and "未知风险等级" in bad_risk["error"]
