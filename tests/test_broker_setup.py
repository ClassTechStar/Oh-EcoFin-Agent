"""本地券商契约网关与实盘自检脚本的测试（不联网、不涉及真实资金）。"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from backend.config import ExecutionConfig
from scripts.check_broker_setup import check_http, check_qmt
from scripts.mock_broker_gateway import create_mock_app


def test_mock_gateway_health_and_idempotent_order():
    client = TestClient(create_mock_app())
    assert client.get("/health").json()["ok"] is True

    body = {"client_order_id": "ord-1", "symbol": "600519", "side": "buy",
            "qty": 100, "order_type": "market", "price": 100.0}
    first = client.post("/orders", json=body).json()
    second = client.post("/orders", json=body).json()
    assert first["status"] == "accepted"
    assert first["broker_order_id"] == second["broker_order_id"]  # 幂等
    assert len(client.get("/orders").json()["orders"]) == 1

    cancel = client.delete(f"/orders/{first['broker_order_id']}")
    assert cancel.status_code == 200 and cancel.json()["ok"] is True
    assert client.delete("/orders/missing").status_code == 404


def test_mock_gateway_filled_mode_and_token():
    client = TestClient(create_mock_app(mode="filled", token="dev-token"))
    body = {"client_order_id": "ord-2", "symbol": "000001", "side": "sell",
            "qty": 200, "price": 10.0}
    assert client.post("/orders", json=body).status_code == 401
    ok = client.post("/orders", json=body,
                     headers={"Authorization": "Bearer dev-token"}).json()
    assert ok["status"] == "filled"
    assert ok["fill"]["qty"] == 200


def test_check_qmt_reports_actionable_problems():
    problems = check_qmt(ExecutionConfig(live_enabled=True, broker_kind="qmt"))
    text = "\n".join(problems)
    assert "资金账号" in text
    assert "miniQMT 路径" in text
    assert "xtquant" in text


def test_check_http_missing_url():
    problems = check_http(ExecutionConfig(live_enabled=True, broker_kind="http"))
    assert problems and "HTTP_BASE_URL" in problems[0]


def test_check_http_with_mock_transport():
    cfg = ExecutionConfig(live_enabled=True, broker_kind="http",
                          http_base_url="https://broker.test", http_token="t")
    ok = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    assert check_http(cfg, transport=ok) == []

    down = httpx.MockTransport(lambda request: httpx.Response(503))
    problems = check_http(cfg, transport=down)
    assert problems and "503" in problems[0]
