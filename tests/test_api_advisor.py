"""M11 投顾合规 API：测评→等级/画像、组合建议→合规四项、收益表述扫描。"""

from __future__ import annotations

import zlib

import pytest
from fastapi.testclient import TestClient

from backend.app import AppContext, create_app
from backend.config import Settings
from backend.quant.fixtures import synthetic_ohlcv

ANSWERS = [
    {"qid": "q1", "choice": "5 年以上"},
    {"qid": "q2", "choice": "接近市场平均（6-10%）"},
    {"qid": "q3", "choice": "按计划继续持有"},
    {"qid": "q4", "choice": "3 年以上"},
    {"qid": "q5", "choice": "6-12 个月"},
]


def make_app(tmp_path) -> tuple[TestClient, AppContext]:
    s = Settings(db_path=str(tmp_path / "advisor.db"))
    s.app.demo_mode = True
    s.llm.daily_cost_cap_cny = 1000
    app = create_app(s)
    return TestClient(app), app.state.ctx


@pytest.fixture()
def api(tmp_path):
    client, ctx = make_app(tmp_path)
    with client:
        async def fake_kline(symbol: str, days: int = 120, adjust: str = "hfq"):
            seed = zlib.crc32(symbol.encode()) % 1000
            return synthetic_ohlcv(days, seed=seed)

        ctx.market.get_kline_raw = fake_kline  # type: ignore[assignment]
        yield client, ctx


def test_assess_full_answers(api):
    client, _ = api
    r = client.post("/api/advisor/assess", json={"answers": ANSWERS})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["score"] == 21
    assert data["level"] == "R4"
    assert data["equity_cap"] == 0.45
    assert data["single_cap"] == 0.25
    assert data["profile"]["label"] == "成长"
    assert data["disclaimer"]


def test_assess_rejects_incomplete_or_duplicate(api):
    client, _ = api
    r = client.post("/api/advisor/assess", json={"answers": ANSWERS[:3]})
    assert r.status_code == 422  # pydantic min_length=5

    dup = ANSWERS[:4] + [ANSWERS[0]]
    r = client.post("/api/advisor/assess", json={"answers": dup})
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    bad_choice = [dict(a) for a in ANSWERS]
    bad_choice[0]["choice"] = "不存在的选项"
    r = client.post("/api/advisor/assess", json={"answers": bad_choice})
    assert r.status_code == 400


def test_statement_check(api):
    client, _ = api
    r = client.post("/api/advisor/statement", json={"text": "本产品保证收益，稳赚不赔"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "attention"
    assert any(v["rule"] == "return_promise" for v in data["violations"])

    ok = client.post("/api/advisor/statement",
                     json={"text": "本组合仅供教学演示，不构成投资建议"}).json()
    assert ok["status"] == "passed"


def test_portfolio_advice_and_compliance(api):
    client, _ = api
    symbols = ["600519", "000001", "601318", "300750", "510300"]
    r = client.post("/api/advisor/portfolio", json={
        "symbols": symbols, "level": "R3",
        "industry": {"600519": "liquor", "000001": "bank",
                     "601318": "insurance", "300750": "battery"},
        "statement": "组合以稳健为主",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert abs(sum(data["weights"].values()) - 1.0) <= 0.02
    assert data["compliance_report"]["status"] in ("passed", "attention")
    assert "disclaimer" in data["compliance_report"]
    assert data["level"] == "R3"


def test_portfolio_r1_and_invalid_level(api):
    client, _ = api
    r = client.post("/api/advisor/portfolio", json={
        "symbols": ["600519", "000001", "601318"], "level": "R1"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False and data["status"] == "attention"

    bad = client.post("/api/advisor/portfolio", json={
        "symbols": ["600519", "000001", "601318"], "level": "R9"})
    assert bad.status_code == 400
