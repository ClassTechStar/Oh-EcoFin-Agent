"""M5 API：报告组合、列表/详情、三种格式下载与错误映射。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings

CSV_TEXT = (
    "date,region,amount\n"
    "2026-01-01,North,100\n"
    "2026-01-02,South,50\n"
    "2026-02-01,North,200\n"
    "2026-02-02,South,150\n"
)


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


def _upload(client) -> dict:
    response = client.post("/api/datasets", files={
        "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
    assert response.status_code == 200
    return response.json()


def _body(dataset: dict) -> dict:
    return {
        "title": "API 报告",
        "subtitle": "导出测试",
        "blocks": [
            {"type": "text", "title": "摘要", "text": "销售稳定。"},
            {"type": "analysis", "title": "趋势",
             "dataset_id": dataset["dataset_id"], "module": "trend",
             "params": {"date_column": "date", "value_column": "amount",
                        "bucket": "month"}},
        ],
    }


def test_compose_list_get_export(api):
    client, _ = api
    dataset = _upload(client)
    response = client.post("/api/reports", json=_body(dataset))
    assert response.status_code == 200
    spec = response.json()
    report_id = spec["report_id"]
    assert spec["spec_hash"]
    assert {f["format"] for f in spec["files"]} == {"html", "docx", "xlsx"}

    listing = client.get("/api/reports").json()
    assert listing["count"] == 1
    assert listing["reports"][0]["report_id"] == report_id
    assert listing["reports"][0]["block_count"] == 2

    detail = client.get(f"/api/reports/{report_id}").json()
    assert detail["title"] == "API 报告"
    assert len(detail["sources"]) == 2

    html = client.get(f"/api/reports/{report_id}/export/html")
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert "API 报告" in html.text

    docx = client.get(f"/api/reports/{report_id}/export/docx")
    assert docx.status_code == 200
    assert docx.content[:2] == b"PK"

    xlsx = client.get(f"/api/reports/{report_id}/export/xlsx")
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"


def test_report_errors(api):
    client, _ = api
    dataset = _upload(client)
    bad = _body(dataset)
    bad["blocks"][1]["params"] = {"columns": ["nope"]}
    bad["blocks"][1]["module"] = "describe"
    response = client.post("/api/reports", json=bad)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "column_not_found"

    assert client.get("/api/reports/rpt_missing").status_code == 404
    assert client.get("/api/reports/rpt_missing/export/html").status_code == 404
    assert client.get("/api/reports/rpt_missing/export/pdf").status_code == 422


def test_report_tool_and_health(api):
    client, ctx = api
    assert "report_compose" in ctx.registry.names()
    health = client.get("/api/health").json()
    assert "reports" in health
