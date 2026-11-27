"""实盘券商适配器：默认关闭、QMT 缺配置 fail-closed、HTTP 网关契约与状态端点。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import ExecutionConfig, Settings
from backend.execution.brokers import (
    HttpBrokerAdapter,
    QmtBrokerAdapter,
    build_live_gateway,
    mask_account,
    stock_code,
)
from backend.execution.gateway import (
    BrokerNotConfiguredError,
    GatewayDisconnectedError,
    GatewayError,
)
from backend.execution.models import AccountContext, OrderRequest


def _order() -> OrderRequest:
    return OrderRequest(symbol="600519", side="buy", qty=100, price=100.0)


def _ctx() -> AccountContext:
    return AccountContext(cash=1_000_000, total_equity=1_000_000)


def _settings(tmp_path, **execution) -> Settings:
    s = Settings(db_path=str(tmp_path / "broker.db"))
    s.app.demo_mode = True
    s.execution = ExecutionConfig(**execution)
    return s


def test_build_live_gateway_disabled_by_default(tmp_path):
    assert build_live_gateway(_settings(tmp_path)) is None


def test_build_live_gateway_rejects_unknown_kind(tmp_path):
    s = _settings(tmp_path, live_enabled=True, broker_kind="magic")
    with pytest.raises(ValueError, match="未知券商适配器"):
        build_live_gateway(s)


async def test_qmt_fails_closed_without_config(tmp_path):
    cfg = ExecutionConfig(live_enabled=True, broker_kind="qmt")
    adapter = QmtBrokerAdapter(cfg)
    with pytest.raises(BrokerNotConfiguredError, match="资金账号"):
        await adapter.connect()
    assert await adapter.health() is False

    cfg2 = ExecutionConfig(live_enabled=True, broker_kind="qmt",
                           account_id="12345678", qmt_path=str(tmp_path / "missing"))
    with pytest.raises(BrokerNotConfiguredError, match="路径不存在"):
        await QmtBrokerAdapter(cfg2).connect()


async def test_http_adapter_contract():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/orders" and request.method == "POST":
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json={
                "broker_order_id": "B-1", "status": "accepted"})
        if request.url.path == "/orders/B-1" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    cfg = ExecutionConfig(live_enabled=True, broker_kind="http",
                          http_base_url="https://broker.test", http_token="secret")
    adapter = HttpBrokerAdapter(cfg, transport=httpx.MockTransport(handler))
    await adapter.connect()
    assert await adapter.health() is True

    ack = await adapter.submit(_order(), _ctx(), market_price=100.0)
    assert ack.status == "accepted" and ack.broker_order_id == "B-1"
    assert seen["auth"] == "Bearer secret"
    assert seen["payload"]["qty"] == 100
    assert seen["payload"]["symbol"] == "600519"

    assert await adapter.cancel("B-1") is True
    assert await adapter.cancel("missing") is False


async def test_http_adapter_error_classes():
    cfg = ExecutionConfig(live_enabled=True, broker_kind="http",
                          http_base_url="https://broker.test", http_token="t")

    def server_error(request):
        return httpx.Response(503)

    adapter = HttpBrokerAdapter(cfg, transport=httpx.MockTransport(server_error))
    with pytest.raises(GatewayDisconnectedError):
        await adapter.submit(_order(), _ctx(), market_price=100.0)

    def bad_request(request):
        return httpx.Response(400, text="bad symbol")

    adapter = HttpBrokerAdapter(cfg, transport=httpx.MockTransport(bad_request))
    with pytest.raises(GatewayError):
        await adapter.submit(_order(), _ctx(), market_price=100.0)


def test_stock_code_and_mask():
    assert stock_code("600519") == "600519.SH"
    assert stock_code("000001") == "000001.SZ"
    assert stock_code("830799") == "830799.BJ"
    assert mask_account("1234567890") == "123***890"
    assert mask_account("123") == "***"
    assert mask_account("") == ""


def test_live_status_endpoint_defaults_off(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        data = client.get("/api/live/status").json()
        assert data["enabled"] is False and data["connected"] is False
        assert "未启用" in data["detail"]
        probe = client.post("/api/live/probe")
        assert probe.status_code == 400


def test_live_status_endpoint_configured(tmp_path):
    s = _settings(tmp_path, live_enabled=True, broker_kind="http",
                  account_id="1234567890", http_base_url="https://broker.test")
    app = create_app(s)
    with TestClient(app) as client:
        data = client.get("/api/live/status").json()
        assert data["enabled"] is True and data["configured"] is True
        assert data["adapter"] == "http"
        assert data["connected"] is False
        assert data["account"] == "123***890"
