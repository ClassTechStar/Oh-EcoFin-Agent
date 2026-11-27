"""M4 API：分析模块目录、运行、错误映射与 Agent 工具面。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings

CSV_TEXT = (
    "date,region,amount,qty\n"
    "2026-01-01,North,100,2\n"
    "2026-01-02,South,50,1\n"
    "2026-02-01,North,200,3\n"
    "2026-02-02,South,150,2\n"
)


@pytest.fixture()
def api(tmp_path):
    settings = Settings(db_path=str(tmp_path / "api.db"))
    settings.app.demo_mode = True
    settings.catalog.dir = str(tmp_path / "catalog")
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    app = create_app(settings)
    client = TestClient(app)
    with client:
        yield client, app.state.ctx


def _upload(client) -> dict:
    response = client.post("/api/datasets", files={
        "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
    assert response.status_code == 200
    return response.json()


def test_modules_endpoint(api):
    client, _ = api
    data = client.get("/api/analysis/modules").json()
    assert data["count"] == 7
    assert {m["name"] for m in data["modules"]} == {
        "describe", "missing", "correlation", "trend", "group", "funnel", "ab_test"}
    trend = next(m for m in data["modules"] if m["name"] == "trend")
    assert any(p["name"] == "bucket" for p in trend["params"])


def test_run_analysis_and_reproduce(api):
    client, _ = api
    dataset = _upload(client)
    body = {
        "module": "trend",
        "dataset_id": dataset["dataset_id"],
        "params": {"date_column": "date", "value_column": "amount",
                   "agg": "sum", "bucket": "month", "group_by": "region"},
    }
    first = client.post("/api/analysis/run", json=body)
    assert first.status_code == 200
    result = first.json()
    assert result["data"]["x"] == ["2026-01", "2026-02"]
    assert result["chart"]["chart_type"] == "line"
    assert result["chart"]["option"]["series"][0]["type"] == "line"

    second = client.post("/api/analysis/run", json=body).json()
    assert second["result_hash"] == result["result_hash"]
    assert second["chart"]["spec_hash"] == result["chart"]["spec_hash"]


def test_run_errors(api):
    client, _ = api
    dataset = _upload(client)
    missing = client.post("/api/analysis/run", json={
        "module": "describe", "dataset_id": dataset["dataset_id"],
        "params": {"columns": ["nope"]}})
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "column_not_found"

    unknown = client.post("/api/analysis/run", json={
        "module": "nope", "dataset_id": dataset["dataset_id"], "params": {}})
    assert unknown.status_code == 400
    assert unknown.json()["detail"]["code"] == "module_not_found"

    bad_params = client.post("/api/analysis/run", json={
        "module": "describe", "dataset_id": dataset["dataset_id"],
        "params": {"top_n": 0}})
    assert bad_params.status_code == 400
    assert bad_params.json()["detail"]["code"] == "invalid_params"


def test_analysis_tools_registered(api):
    client, ctx = api
    assert {"analysis_modules", "analysis_run"} <= set(ctx.registry.names())
    health = client.get("/api/health").json()
    assert health["analysis"]["modules"] == 7
