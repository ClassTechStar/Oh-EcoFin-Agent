"""M7 可信审计：证据账本、数字抽取/比对、阻断与重跑。"""

from __future__ import annotations

import pytest

from backend.analysis.service import AnalysisService
from backend.audit.numbers import extract_claims, parse_cell_number
from backend.config import Settings
from backend.datasets.service import DatasetService
from backend.query.engine import QueryAuditStore, QueryEngine
from backend.reports.models import ReportAuditError, ReportComposeRequest
from backend.reports.service import ReportService

CSV_TEXT = (
    "date,region,amount\n"
    "2026-01-01,North,100\n"
    "2026-01-02,South,50\n"
    "2026-02-01,North,600\n"
    "2026-02-02,South,550\n"
)


@pytest.fixture()
def env(tmp_path, tmp_db):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    settings.reports.dir = str(tmp_path / "reports")
    datasets = DatasetService(tmp_db, settings)
    csv = tmp_path / "sales.csv"
    csv.write_text(CSV_TEXT, encoding="utf-8")
    dataset = datasets.import_path(csv, name="sales")
    analysis = AnalysisService(datasets, settings)
    query = QueryEngine(datasets, QueryAuditStore(tmp_db),
                        settings.datasets.workspace_dir,
                        max_rows=100, timeout_seconds=10)
    service = ReportService(tmp_db, settings, analysis, query)
    return service, dataset, tmp_path


def _request(dataset: dict, text: str) -> ReportComposeRequest:
    return ReportComposeRequest.model_validate({
        "title": "销售审计",
        "subtitle": "M7",
        "blocks": [
            {"type": "text", "title": "摘要", "text": text},
            {"type": "analysis", "title": "区域销售",
             "dataset_id": dataset["dataset_id"], "module": "group",
             "params": {"group_column": "region", "value_column": "amount",
                        "agg": "sum"}},
        ],
    })


def test_number_extraction_rules():
    claims = extract_claims(
        "增长 20%，金额 1,234 万元，日期 2026-01-01，代码 600519\n"
        "1. 第一项\n<script>alert(1)</script>")
    values = {(c["value"], c["unit"]) for c in claims}
    assert (20.0, "%") in values
    assert (12340000.0, "万") in values
    assert all(c["value"] != 600519 for c in claims)
    assert all(c["value"] != 1 for c in claims)
    assert extract_claims("<script>alert(1)</script>") == []
    attached = extract_claims("日期2026-01-01，下降-5%")
    assert any(c["value"] == -5.0 and c["unit"] == "%" for c in attached)
    assert all(c["value"] not in (2026.0, 1.0) for c in attached)
    assert parse_cell_number("53.85%") == (0.5385, "%")
    assert parse_cell_number("1,234") == (1234.0, "")
    assert parse_cell_number("North") is None


def test_valid_report_passes_and_reruns_identically(env):
    service, dataset, tmp_path = env
    spec = service.compose(_request(
        dataset, "北方区域销售额 700 元，占比 53.85%。"))
    assert spec["status"] == "ready"
    assert spec["audit"]["status"] == "passed"
    assert spec["audit"]["claims_failed"] == 0
    assert spec["audit"]["evidence_count"] > 0
    assert spec["manifest"]["spec_hash"] == spec["spec_hash"]
    assert len(spec["files"]) == 3

    detail = service.audit.get(spec["report_id"])
    assert detail["evidence_count"] > 0
    assert detail["claims_total"] > 0
    text_claims = [c for c in detail["claims"] if c["claim"] != "结构化数值"]
    assert text_claims
    assert all(c["passed"] for c in text_claims)
    assert all(c["evidence_ids"] for c in text_claims)

    rerun = service.rerun(spec["report_id"])
    assert rerun["same"] is True
    assert rerun["differences"] == []
    assert rerun["new_spec_hash"] == spec["spec_hash"]
    new_dir = tmp_path / "reports" / rerun["report_id"]
    assert (new_dir / "report.html").is_file()


def test_unsupported_number_blocks_report(env):
    service, dataset, tmp_path = env
    with pytest.raises(ReportAuditError) as exc:
        service.compose(_request(dataset, "北方区域销售额 99999 元。"))
    error = exc.value
    assert error.code == "audit_blocked"
    assert error.report_id.startswith("rpt_")
    assert error.audit["status"] == "blocked"
    assert error.audit["claims_failed"] >= 1

    payload = service.store.get(error.report_id)
    assert payload["status"] == "blocked"
    assert payload["spec"]["status"] == "blocked"
    assert payload["files"] == []
    report_dir = tmp_path / "reports" / error.report_id
    assert (report_dir / "spec.json").is_file()
    assert not (report_dir / "report.html").exists()

    detail = service.audit.get(error.report_id)
    failed = [c for c in detail["claims"] if not c["passed"]]
    assert failed and failed[0]["claim"] == "99999"

    rerun = service.rerun(error.report_id)
    assert rerun["blocked"] is True
    assert rerun["same"] is False


def test_script_text_does_not_create_claims(env):
    service, dataset, _ = env
    spec = service.compose(_request(
        dataset, "<script>alert(1)</script> 本周销售平稳。"))
    assert spec["audit"]["status"] == "passed"
    assert spec["audit"]["claims_failed"] == 0
