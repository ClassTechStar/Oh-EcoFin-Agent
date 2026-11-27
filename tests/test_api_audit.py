"""M7 API：审计列表/详情、阻断报告与重跑。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings

CSV_TEXT = (
    "date,region,amount\n"
    "2026-01-01,North,100\n"
    "2026-01-02,South,50\n"
    "2026-02-01,North,600\n"
    "2026-02-02,South,550\n"
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


def _dataset(client) -> dict:
    response = client.post("/api/datasets", files={
        "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
    assert response.status_code == 200
    return response.json()


def _body(dataset: dict, text: str) -> dict:
    return {
        "title": "审计 API 报告",
        "blocks": [
            {"type": "text", "title": "摘要", "text": text},
            {"type": "analysis", "title": "区域",
             "dataset_id": dataset["dataset_id"], "module": "group",
             "params": {"group_column": "region", "value_column": "amount",
                        "agg": "sum"}},
        ],
    }


def test_audit_passed_report_and_rerun(api):
    client, _ = api
    dataset = _dataset(client)
    response = client.post("/api/reports", json=_body(
        dataset, "北方销售额 700 元。"))
    assert response.status_code == 200
    spec = response.json()
    assert spec["status"] == "ready"
    assert spec["audit"]["status"] == "passed"

    listing = client.get("/api/audit/reports").json()
    assert listing["count"] == 1
    row = listing["reports"][0]
    assert row["audit_status"] == "passed"
    assert row["evidence_count"] > 0
    assert row["claims_failed"] == 0

    detail = client.get(f"/api/audit/reports/{spec['report_id']}").json()
    assert detail["audit"]["status"] == "passed"
    assert detail["report"]["report_id"] == spec["report_id"]
    assert detail["evidence"] and detail["claims"]
    assert all(c["passed"] for c in detail["claims"] if c["claim"] != "结构化数值")

    rerun = client.post(f"/api/reports/{spec['report_id']}/rerun").json()
    assert rerun["same"] is True
    assert rerun["differences"] == []


def test_audit_blocked_report(api):
    client, _ = api
    dataset = _dataset(client)
    response = client.post("/api/reports", json=_body(
        dataset, "北方销售额 99999 元。"))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "audit_blocked"
    assert detail["report_id"]
    assert detail["audit"]["status"] == "blocked"
    assert detail["audit"]["claims_failed"] >= 1

    report_id = detail["report_id"]
    report = client.get(f"/api/reports/{report_id}").json()
    assert report["status"] == "blocked"
    assert report["files"] == []

    audit = client.get(f"/api/audit/reports/{report_id}").json()
    failed = [c for c in audit["claims"] if not c["passed"]]
    assert failed and failed[0]["claim"] == "99999"

    assert client.get(f"/api/reports/{report_id}/export/html").status_code == 404
