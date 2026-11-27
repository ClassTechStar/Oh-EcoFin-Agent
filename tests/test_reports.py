"""M5 报告：report spec、HTML/Word/Excel、图表引用与可复现哈希。"""

from __future__ import annotations

import json

import openpyxl
import pytest
from docx import Document

from backend.analysis.service import AnalysisService
from backend.config import Settings
from backend.datasets.service import DatasetService
from backend.query.engine import QueryAuditStore, QueryEngine
from backend.reports.models import ReportComposeRequest, ReportError
from backend.reports.service import ReportService

CSV_TEXT = (
    "date,region,amount,qty,converted,stage\n"
    "2026-01-01,North,100,2,1,visit\n"
    "2026-01-01,South,50,1,0,visit\n"
    "2026-01-02,North,200,3,1,cart\n"
    "2026-01-02,South,150,2,1,cart\n"
    "2026-01-03,North,,1,0,pay\n"
    "2026-01-03,South,300,4,1,pay\n"
    "2026-02-01,North,400,5,1,visit\n"
    "2026-02-02,South,100,1,0,visit\n"
)


@pytest.fixture()
def report_env(tmp_path, tmp_db):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    settings.reports.dir = str(tmp_path / "reports")
    datasets = DatasetService(tmp_db, settings)
    csv = tmp_path / "trades.csv"
    csv.write_text(CSV_TEXT, encoding="utf-8")
    dataset = datasets.import_path(csv, name="trades")
    analysis = AnalysisService(datasets, settings)
    query = QueryEngine(datasets, QueryAuditStore(tmp_db),
                        settings.datasets.workspace_dir,
                        max_rows=100, timeout_seconds=10)
    service = ReportService(tmp_db, settings, analysis, query)
    return service, dataset, settings


def _request(dataset: dict) -> ReportComposeRequest:
    return ReportComposeRequest.model_validate({
        "title": "区域销售周报",
        "subtitle": "M5 报告导出烟测",
        "blocks": [
            {"type": "text", "title": "结论摘要",
             "text": "本周销售平稳。\n\n<script>alert(1)</script>"},
            {"type": "analysis", "title": "区域销售",
             "dataset_id": dataset["dataset_id"], "module": "group",
             "params": {"group_column": "region", "value_column": "amount",
                        "agg": "sum"}},
            {"type": "analysis", "title": "月度趋势",
             "dataset_id": dataset["dataset_id"], "module": "trend",
             "params": {"date_column": "date", "value_column": "amount",
                        "bucket": "month", "group_by": "region"}},
            {"type": "sql", "title": "区域明细",
             "dataset_id": dataset["dataset_id"],
             "sql": f"SELECT region, SUM(amount) AS total FROM {dataset['table_name']}"
                    " GROUP BY region ORDER BY total DESC",
             "limit": 50},
        ],
    })


def test_compose_all_formats(report_env, tmp_path):
    service, dataset, _ = report_env
    spec = service.compose(_request(dataset))
    assert spec["report_id"].startswith("rpt_")
    assert len(spec["blocks"]) == 4
    assert len(spec["sources"]) == 4
    assert len(spec["datasets"]) == 1
    assert spec["datasets"][0]["sha256"] == dataset["sha256"]
    assert {f["format"] for f in spec["files"]} == {"html", "docx", "xlsx"}
    assert spec["spec_hash"]
    assert all(b["source"]["result_hash"] for b in spec["blocks"][1:])

    report_dir = tmp_path / "reports" / spec["report_id"]
    chart = report_dir / "charts" / "b2.png"
    assert chart.is_file()
    assert chart.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    html = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "区域销售周报" in html
    assert "data:image/png;base64" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "http://" not in html and "https://" not in html
    assert spec["spec_hash"] in html

    docx = Document(report_dir / "report.docx")
    assert len(docx.tables) >= 4
    assert len(docx.inline_shapes) >= 2

    workbook = openpyxl.load_workbook(report_dir / "report.xlsx")
    assert "报告概览" in workbook.sheetnames
    assert sum(len(ws._charts) for ws in workbook.worksheets) >= 2
    assert workbook["区域销售"]["A1"].value


def test_spec_hash_is_reproducible(report_env):
    service, dataset, _ = report_env
    first = service.compose(_request(dataset))
    second = service.compose(_request(dataset))
    assert first["spec_hash"] == second["spec_hash"]
    assert first["report_id"] != second["report_id"]
    assert [b["source"]["result_hash"] for b in first["blocks"]] == [
        b["source"]["result_hash"] for b in second["blocks"]]


def test_text_only_report(report_env):
    service, _, _ = report_env
    spec = service.compose(ReportComposeRequest.model_validate({
        "title": "纯文字报告",
        "blocks": [{"type": "text", "title": "说明", "text": "没有数据块。"}],
    }))
    assert spec["datasets"] == []
    assert len(spec["files"]) == 3
    assert spec["sources"][0]["type"] == "narrative"


def test_report_queries_and_export(report_env, tmp_path):
    service, dataset, _ = report_env
    spec = service.compose(_request(dataset))
    loaded = service.get(spec["report_id"])
    assert loaded["title"] == "区域销售周报"
    assert all("path" not in f for f in loaded["files"])
    rows = service.list()
    assert rows[0]["report_id"] == spec["report_id"]
    assert rows[0]["block_count"] == 4
    assert all("path" not in f for f in rows[0]["files"])

    for fmt in ("html", "docx", "xlsx"):
        path, filename = service.export_path(spec["report_id"], fmt)
        assert path.is_file()
        assert filename.endswith(f".{fmt}")


def test_compose_errors(report_env):
    service, dataset, settings = report_env
    with pytest.raises(ReportError) as exc:
        service.compose(ReportComposeRequest.model_validate({
            "title": "坏报告",
            "blocks": [{"type": "analysis", "dataset_id": dataset["dataset_id"],
                        "module": "describe", "params": {"columns": ["nope"]}}],
        }))
    assert exc.value.code == "column_not_found"

    with pytest.raises(ReportError) as exc:
        service.compose(ReportComposeRequest.model_validate({
            "title": "坏 SQL",
            "blocks": [{"type": "sql", "sql": "DROP TABLE trades"}],
        }))
    assert exc.value.code == "not_read_only"

    settings.reports.max_blocks = 2
    with pytest.raises(ReportError) as exc:
        service.compose(_request(dataset))
    assert exc.value.code == "too_many_blocks"

    with pytest.raises(ReportError) as exc:
        service.get("rpt_missing")
    assert exc.value.code == "report_not_found"


def test_spec_json_on_disk(report_env, tmp_path):
    service, dataset, _ = report_env
    spec = service.compose(_request(dataset))
    path = tmp_path / "reports" / spec["report_id"] / "spec.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["spec_hash"] == spec["spec_hash"]
    assert saved["files"] == spec["files"]
