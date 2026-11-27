"""交易看板 API：M7 信号预览/落库 + M10 仿真账户/下单/风控事件。"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.app import AppContext, create_app
from backend.config import Settings
from backend.quant.fixtures import synthetic_ohlcv


def _snapshot_row(symbol: str = "600519", price: float = 100.0,
                  pct: float = 2.0) -> dict:
    return {
        "代码": symbol, "名称": "贵州茅台", "最新价": price, "涨跌幅": pct,
        "成交量": 1_000_000, "成交额": 1e8, "换手率": 1.2, "量比": 1.1,
        "总市值": 2e12, "市盈率-动态": 30.0, "市净率": 8.0,
    }


def _uptrend_kline() -> pd.DataFrame:
    """确定性上行日线：保证动量/低波模型给出正向观点（避免随机种子方向依赖）。"""
    df = synthetic_ohlcv(days=220, seed=11)
    closes = [100.0 * (1.001 ** i) for i in range(len(df))]
    df["close"] = closes
    df["open"] = [c * 0.999 for c in closes]
    df["high"] = [c * 1.004 for c in closes]
    df["low"] = [c * 0.996 for c in closes]
    return df


def make_app(tmp_path) -> tuple[TestClient, AppContext]:
    s = Settings(db_path=str(tmp_path / "trading.db"))
    s.app.demo_mode = True
    s.llm.daily_cost_cap_cny = 1000
    app = create_app(s)
    return TestClient(app), app.state.ctx


@pytest.fixture()
def api(tmp_path):
    client, ctx = make_app(tmp_path)
    with client:
        ctx.snapshot._swap(pd.DataFrame([_snapshot_row()]), "test")
        trend = _uptrend_kline()

        async def fake_kline(symbol: str, days: int = 120, adjust: str = "hfq"):
            return trend

        ctx.market.get_kline_raw = fake_kline  # type: ignore[assignment]
        yield client, ctx


def test_signal_preview_and_history(api):
    client, ctx = api
    r = client.get("/api/signals/600519")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["symbol"] == "600519"
    assert data["as_of"] == str(synthetic_ohlcv(days=220, seed=7)["date"].iloc[-1])[:10]
    assert len(data["signals"]) == 3
    assert data["saved_signals"] == 3
    assert data["weights"]["600519"] > 0
    assert data["constraints"][0]["cap"] == 0.25
    assert data["disclaimer"]

    history = client.get("/api/signals/history?symbol=600519&limit=10").json()
    assert len(history["signals"]) == 3
    assert {s["model_name"] for s in history["signals"]} == {
        "momentum_120d", "reversal_5d", "lowvol_60d"}

    assert client.get("/api/signals/abc").status_code == 400
    assert client.get("/api/signals/600519?mode=bad").status_code == 400


def test_paper_quote_and_account(api):
    client, _ = api
    quote = client.get("/api/paper/quote/600519").json()["quote"]
    assert quote["price"] == 100.0
    assert quote["limit_up"] == pytest.approx(107.84)
    assert quote["limit_down"] == pytest.approx(88.24)
    assert quote["suspended"] is False

    account = client.get("/api/paper/account?risk_level=R3").json()
    assert account["cash"] == 1_000_000
    assert account["equity"] == 1_000_000
    assert account["positions"] == []
    assert account["reconcile"]["ok"] is True
    assert account["caps"]["single"]["R3"] == 0.2

    assert client.get("/api/paper/account?risk_level=R9").status_code == 400
    assert client.get("/api/paper/quote/000001").status_code == 404


def test_paper_order_t1_flow_and_ledger(api):
    client, ctx = api
    buy = client.post("/api/paper/orders", json={
        "symbol": "600519", "side": "buy", "qty": 100,
        "risk_level": "R3", "strategy_id": "manual"}).json()
    assert buy["ok"] is True, buy
    assert buy["order"]["fill"]["qty"] == 100
    assert buy["account"]["positions"][0]["available_qty"] == 0

    # T+1：当日买入不可卖 → G2 拒绝
    sell = client.post("/api/paper/orders", json={
        "symbol": "600519", "side": "sell", "qty": 100,
        "risk_level": "R3"}).json()
    assert sell["ok"] is False
    assert "T+1" in sell["order"]["reason"]

    rolled = client.post("/api/paper/roll-day?risk_level=R3").json()
    assert rolled["ok"] is True
    assert rolled["account"]["positions"][0]["available_qty"] == 100

    sell2 = client.post("/api/paper/orders", json={
        "symbol": "600519", "side": "sell", "qty": 100,
        "risk_level": "R3"}).json()
    assert sell2["ok"] is True
    assert sell2["account"]["positions"] == []
    assert sell2["account"]["reconcile"]["ok"] is True

    orders = client.get("/api/paper/orders").json()["orders"]
    assert len(orders) == 3
    assert {o["status"] for o in orders} == {"filled", "rejected"}

    events = client.get("/api/paper/risk-events").json()["events"]
    assert len(events) >= 3
    assert {e["gate"] for e in events} >= {"G1_suitability", "G2_capital"}

    assert ctx.broker.reconcile({"600519": 100.0})["ok"] is True


def test_paper_r1_blocks_equity_buy(api):
    client, ctx = api
    r = client.post("/api/paper/orders", json={
        "symbol": "600519", "side": "buy", "qty": 100,
        "risk_level": "R1"}).json()
    assert r["ok"] is False
    assert "R1" in r["order"]["reason"]
    assert ctx.broker.positions == {}
    events = client.get("/api/paper/risk-events").json()["events"]
    assert any(e["level"] == "block" and e["gate"] == "G1_suitability"
               for e in events)


def test_paper_limit_up_rejected_by_broker(tmp_path):
    client, ctx = make_app(tmp_path)
    with client:
        ctx.snapshot._swap(pd.DataFrame([_snapshot_row(pct=10.0)]), "test")
        r = client.post("/api/paper/orders", json={
            "symbol": "600519", "side": "buy", "qty": 100,
            "risk_level": "R3"}).json()
        assert r["ok"] is False
        assert "涨停" in r["order"]["reason"]
        events = client.get("/api/paper/risk-events").json()["events"]
        assert any(e["rule"] == "match" and e["level"] == "block" for e in events)
