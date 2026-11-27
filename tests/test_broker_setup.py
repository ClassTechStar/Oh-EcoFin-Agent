"""本地券商契约网关与实盘自检脚本的测试（不联网、不涉及真实资金）。"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from backend.config import ExecutionConfig
from backend.execution.brokers import resolve_http_token
from scripts.check_broker_setup import check_http, check_qmt
from scripts.configure_broker import main as configure_main
from scripts.configure_broker import upsert_env
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


def test_resolve_http_token_prefers_config():
    cfg = ExecutionConfig(http_token="from-config")
    assert resolve_http_token(cfg) == "from-config"


def test_upsert_env_preserves_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# 注释\nFINAGENT_GLM_KEY=k\n"
                    "FINAGENT_EXECUTION__BROKER_KIND=qmt\n", encoding="utf-8")
    upsert_env(path, {
        "FINAGENT_EXECUTION__BROKER_KIND": "http",
        "FINAGENT_EXECUTION__HTTP_BASE_URL": "https://broker.example",
    })
    text = path.read_text(encoding="utf-8")
    assert "# 注释" in text and "FINAGENT_GLM_KEY=k" in text
    assert text.count("FINAGENT_EXECUTION__BROKER_KIND=") == 1
    assert "FINAGENT_EXECUTION__BROKER_KIND=http" in text
    assert "FINAGENT_EXECUTION__HTTP_BASE_URL=https://broker.example" in text


def test_configure_http_writes_env_without_probe(tmp_path):
    env_file = tmp_path / ".env"
    rc = configure_main([
        "--kind", "http", "--env-file", str(env_file),
        "--base-url", "https://broker.example", "--account", "1234567890",
        "--token", "tok-123", "--no-probe", "--store", "file",
    ])
    assert rc == 0
    text = env_file.read_text(encoding="utf-8")
    assert "FINAGENT_EXECUTION__LIVE_ENABLED=1" in text
    assert "FINAGENT_EXECUTION__BROKER_KIND=http" in text
    assert "FINAGENT_EXECUTION__HTTP_BASE_URL=https://broker.example" in text
    assert "FINAGENT_EXECUTION__HTTP_TOKEN=tok-123" in text
    assert "FINAGENT_EXECUTION__ACCOUNT_ID=1234567890" in text


def test_configure_qmt_rejects_missing_path(tmp_path):
    env_file = tmp_path / ".env"
    rc = configure_main([
        "--kind", "qmt", "--env-file", str(env_file),
        "--account", "1234567890", "--qmt-path", str(tmp_path / "missing"),
    ])
    assert rc == 1
    assert not env_file.exists()
