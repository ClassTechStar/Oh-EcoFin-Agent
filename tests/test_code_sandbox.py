"""P3 自由代码沙箱：正常执行、输出、拒绝矩阵、超时、禁用与 API。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.datasets.service import DatasetService
from backend.db import Database
from backend.sandbox.models import (
    SandboxDisabledError,
    SandboxValidationError,
)
from backend.sandbox.service import SandboxService

CSV_TEXT = (
    "date,region,amount\n"
    "2026-01-01,North,100.5\n"
    "2026-01-02,South,200\n"
    "2026-01-03,North,50\n"
)

GOOD_CODE = """
df = datasets['sales']
totals = df.groupby('region')['amount'].sum().to_dict()
result = {'rows': len(df), 'totals': totals}
save_output('summary.json', result)
print('sandbox ok')
"""


def make_settings(tmp_path) -> Settings:
    settings = Settings(db_path=str(tmp_path / "sandbox.db"))
    settings.app.demo_mode = True
    settings.catalog.dir = str(tmp_path / "catalog")
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    settings.reports.dir = str(tmp_path / "reports")
    settings.sandbox.dir = str(tmp_path / "sandbox")
    settings.sandbox.enabled = True
    return settings


@pytest.fixture()
def env(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    datasets = DatasetService(db, settings)
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(CSV_TEXT, encoding="utf-8")
    imported = datasets.import_path(csv_path, "sales")
    service = SandboxService(db, settings, datasets)
    return settings, db, datasets, service, imported


def test_run_success_with_output(env):
    settings, _, _, service, imported = env
    record = service.run(GOOD_CODE, [imported["dataset_id"]])

    assert record["status"] == "ok", record["error"]
    assert record["exit_code"] == 0
    assert record["result"] == {
        "rows": 3,
        "totals": {"North": 150.5, "South": 200.0},
    }
    assert record["stdout"] == "sandbox ok\n"
    assert len(record["files"]) == 1
    assert record["files"][0]["name"] == "summary.json"
    assert record["files"][0]["bytes"] > 0
    target = (Path(settings.sandbox.dir) / "runs" / record["run_id"]
              / "files" / "summary.json")
    assert target.is_file()

    detail = service.get(record["run_id"])
    assert detail["status"] == "ok"
    assert detail["datasets"][0]["dataset_id"] == imported["dataset_id"]
    assert service.recent(10)[0]["run_id"] == record["run_id"]
    assert service.count() == 1


@pytest.mark.parametrize("code", [
    "import os",
    "import socket",
    "import subprocess",
    "import pathlib",
    "from os import path",
    "open('/tmp/escape.txt', 'w')",
    "__import__('os')",
    "pd.read_csv('sales.csv')",
    "df = datasets['sales']\ndf.to_csv('escape.csv')",
    "x = (1).__class__",
    "x = 'C:\\\\Windows\\\\system.ini'",
    "x = 'data/../../secret.parquet'",
])
def test_reject_dangerous_code(env, code):
    _, _, _, service, _ = env
    with pytest.raises(SandboxValidationError):
        service.run(code)


def test_timeout_kills_worker(env):
    settings, _, _, service, _ = env
    settings.sandbox.timeout_seconds = 3
    record = service.run("while True:\n    pass")
    assert record["status"] == "timeout"
    assert "超时" in record["error"]
    assert record["duration_ms"] >= 1000


def test_disabled_service(env):
    settings, _, _, service, _ = env
    settings.sandbox.enabled = False
    with pytest.raises(SandboxDisabledError):
        service.run("result = 1")


def test_output_limit(env):
    settings, _, _, service, _ = env
    settings.sandbox.max_files = 1
    over = service.run(
        "save_output('a.json', {'x': 1})\n"
        "save_output('b.json', {'x': 2})\n")
    assert over["status"] == "error"
    assert "文件数超过限额" in over["error"]
    assert [f["name"] for f in over["files"]] == ["a.json"]


def test_save_output_path_traversal_rejected(env):
    _, _, _, service, _ = env
    with pytest.raises(SandboxValidationError):
        service.run("save_output('../escape.json', {'x': 1})")


def test_code_length_limit(env):
    settings, _, _, service, _ = env
    settings.sandbox.max_code_chars = 20
    with pytest.raises(SandboxValidationError):
        service.run("result = '" + "x" * 100 + "'")


def test_api_and_tool_registration(tmp_path):
    settings = make_settings(tmp_path)
    settings.sandbox.timeout_seconds = 10
    app = create_app(settings)
    with TestClient(app) as client:
        ctx = app.state.ctx
        assert ctx.registry.get("run_sandbox_code") is not None
        assert ctx.react._allow_side_effect("run_sandbox_code", "用代码计算") is True
        assert ctx.react._allow_side_effect("run_sandbox_code", "分析一下") is False

        upload = client.post("/api/datasets", files={
            "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
        assert upload.status_code == 200

        status = client.get("/api/sandbox/status").json()
        assert status["enabled"] is True
        assert status["limits"]["memory_mb"] >= 512

        response = client.post("/api/sandbox/run", json={"code": GOOD_CODE})
        assert response.status_code == 200, response.text
        payload = response.json()
        run_id = payload["run_id"]
        assert payload["status"] == "ok"
        assert payload["result"]["totals"]["South"] == 200.0

        detail = client.get(f"/api/sandbox/runs/{run_id}").json()
        assert detail["run"]["run_id"] == run_id
        listed = client.get("/api/sandbox/runs").json()
        assert listed["count"] == 1

        download = client.get(
            f"/api/sandbox/runs/{run_id}/files/summary.json")
        assert download.status_code == 200
        assert b"North" in download.content

        rejected = client.post("/api/sandbox/run",
                               json={"code": "import socket"})
        assert rejected.status_code == 400
        assert rejected.json()["detail"]["code"] == "sandbox_rejected"


def test_api_disabled(tmp_path):
    settings = make_settings(tmp_path)
    settings.sandbox.enabled = False
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.ctx.registry.get("run_sandbox_code") is None
        assert client.get("/api/sandbox/status").json()["enabled"] is False
        response = client.post("/api/sandbox/run", json={"code": "result = 1"})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "sandbox_disabled"
