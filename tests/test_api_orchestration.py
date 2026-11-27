"""M6 API：SSE 单一终态、运行记录与副作用元数据。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.engine.gateway import RecoverableLLMError


@pytest.fixture()
def api(tmp_path):
    settings = Settings(db_path=str(tmp_path / "api.db"))
    settings.app.demo_mode = True
    settings.catalog.dir = str(tmp_path / "catalog")
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    settings.reports.dir = str(tmp_path / "reports")
    app = create_app(settings)
    client = TestClient(app)
    with client:
        yield client, app.state.ctx


def test_chat_error_still_has_single_terminal(api):
    client, ctx = api

    class FailingGW:
        async def chat(self, *args, **kwargs):
            raise RecoverableLLMError("模型暂不可用")

    ctx.react.gw = FailingGW()
    response = client.post("/api/chat", json={"message": "你好", "mode": "chat"})
    assert response.status_code == 200
    body = response.text
    assert body.count("event: done") == 1
    assert body.count("event: error") == 1
    assert "llm_unavailable" in body
    assert "event: run_state" in body

    runs = client.get("/api/runs").json()
    assert runs["count"] == 1
    assert runs["runs"][0]["stop_reason"] == "llm_unavailable"
    assert runs["runs"][0]["state"] == "failed"

    run_id = runs["runs"][0]["id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["id"] == run_id
    assert client.get("/api/runs/run_missing").status_code == 404


def test_health_exposes_side_effect_policy(api):
    client, _ = api
    data = client.get("/api/health").json()
    policy = data["agent"]["side_effects"]
    assert policy["submit_paper_order"]["side_effect"] == "external"
    assert policy["submit_paper_order"]["confirmation"] == "explicit_request"
    assert policy["report_compose"]["side_effect"] == "workspace"
    assert policy["get_quote"]["side_effect"] == "none"
