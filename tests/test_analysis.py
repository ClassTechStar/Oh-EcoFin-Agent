"""M4 分析模块：金样输出、图表 spec 复现、边界错误。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.analysis.common import canonical_json
from backend.analysis.models import AnalysisError
from backend.analysis.service import AnalysisService
from backend.config import Settings
from backend.datasets.service import DatasetService

GOLDEN = Path(__file__).resolve().parent / "golden" / "analysis"

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

CASES: dict[str, dict] = {
    "describe": {},
    "missing": {},
    "correlation": {},
    "trend": {"date_column": "date", "value_column": "amount", "agg": "sum",
              "bucket": "month", "group_by": "region"},
    "group": {"group_column": "region", "value_column": "amount", "agg": "sum"},
    "funnel": {"stage_column": "stage", "stages": ["visit", "cart", "pay"]},
    "ab_test": {"group_column": "region", "metric_column": "converted"},
}


@pytest.fixture()
def analysis(tmp_path, tmp_db):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    datasets = DatasetService(tmp_db, settings)
    csv = tmp_path / "trades.csv"
    csv.write_text(CSV_TEXT, encoding="utf-8")
    dataset = datasets.import_path(csv, name="trades")
    return AnalysisService(datasets, settings), dataset


def test_module_catalog(analysis):
    service, _ = analysis
    modules = service.modules()
    assert {m["name"] for m in modules} == set(CASES)
    trend = next(m for m in modules if m["name"] == "trend")
    date_param = next(p for p in trend["params"] if p["name"] == "date_column")
    assert date_param["required"] is True
    assert "date" in date_param["column_types"]


@pytest.mark.parametrize("module", list(CASES))
def test_golden_outputs(analysis, module):
    service, dataset = analysis
    result = service.run(module, dataset["dataset_id"], CASES[module])
    golden = json.loads((GOLDEN / f"{module}.json").read_text(encoding="utf-8"))
    actual = json.loads(canonical_json(result["data"]))
    assert actual == golden
    assert result["result_hash"]
    assert json.dumps(result["data"], ensure_ascii=False)


def test_chart_specs(analysis):
    service, dataset = analysis
    expected = {
        "missing": "bar",
        "correlation": "heatmap",
        "trend": "line",
        "group": "bar",
        "funnel": "funnel",
        "ab_test": "bar",
    }
    for module, chart_type in expected.items():
        result = service.run(module, dataset["dataset_id"], CASES[module])
        chart = result["chart"]
        assert chart["chart_type"] == chart_type
        assert chart["spec_hash"] and chart["result_hash"] == result["result_hash"]
        assert chart["dataset_sha256"] == dataset["sha256"]
        assert chart["option"]["series"]
    assert service.run("describe", dataset["dataset_id"])["chart"] is None


def test_result_and_chart_hashes_are_reproducible(analysis):
    service, dataset = analysis
    first = service.run("trend", dataset["dataset_id"], CASES["trend"])
    second = service.run("trend", dataset["dataset_id"], CASES["trend"])
    assert first["result_hash"] == second["result_hash"]
    assert first["chart"]["spec_hash"] == second["chart"]["spec_hash"]


def test_trend_count_without_value_column(analysis):
    service, dataset = analysis
    result = service.run("trend", dataset["dataset_id"], {
        "date_column": "date", "agg": "count", "bucket": "day"})
    assert result["data"]["x"] == [
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-02-01", "2026-02-02"]
    assert result["data"]["series"][0]["values"] == [2, 2, 2, 1, 1]


def test_ab_mean_and_one_sided(analysis):
    service, dataset = analysis
    mean = service.run("ab_test", dataset["dataset_id"], {
        "group_column": "region", "metric_column": "amount"})
    assert mean["data"]["metric_type"] == "mean"
    assert mean["data"]["test"] == "welch_t"
    assert 0 <= mean["data"]["p_value"] <= 1
    one_sided = service.run("ab_test", dataset["dataset_id"], {
        "group_column": "region", "metric_column": "converted",
        "baseline": "South", "alternative": "greater"})
    assert one_sided["data"]["metric_type"] == "proportion"
    assert one_sided["data"]["baseline"]["label"] == "South"
    assert one_sided["data"]["diff"] == pytest.approx(0.25)


def test_funnel_sum_basis(analysis):
    service, dataset = analysis
    result = service.run("funnel", dataset["dataset_id"], {
        "stage_column": "stage", "value_column": "amount"})
    assert result["data"]["basis"] == "sum"
    assert result["data"]["items"][0]["value"] == 650.0
    assert result["chart"]["chart_type"] == "funnel"


def test_group_pie_negative_falls_back_to_bar(analysis, tmp_path):
    service, _ = analysis
    csv = tmp_path / "neg.csv"
    csv.write_text("k,v\na,10\nb,-5\n", encoding="utf-8")
    dataset = service.datasets.import_path(csv, name="neg")
    result = service.run("group", dataset["dataset_id"], {
        "group_column": "k", "value_column": "v", "agg": "sum",
        "chart_type": "pie"})
    assert result["chart"]["chart_type"] == "bar"
    assert any("饼图" in w for w in result["warnings"])


def test_errors(analysis, tmp_path):
    service, dataset = analysis
    with pytest.raises(AnalysisError) as exc:
        service.run("nope", dataset["dataset_id"], {})
    assert exc.value.code == "module_not_found"

    with pytest.raises(AnalysisError) as exc:
        service.run("describe", "ds_missing", {})
    assert exc.value.code == "dataset_not_found"

    with pytest.raises(AnalysisError) as exc:
        service.run("describe", dataset["dataset_id"], {"columns": ["nope"]})
    assert exc.value.code == "column_not_found"
    assert "amount" in exc.value.hint

    with pytest.raises(AnalysisError) as exc:
        service.run("correlation", dataset["dataset_id"],
                    {"columns": ["region", "stage"]})
    assert exc.value.code == "column_type"

    with pytest.raises(AnalysisError) as exc:
        service.run("ab_test", dataset["dataset_id"],
                    {"group_column": "stage", "metric_column": "amount"})
    assert exc.value.code == "group_count"

    with pytest.raises(AnalysisError) as exc:
        service.run("trend", dataset["dataset_id"],
                    {"date_column": "date", "agg": "sum"})
    assert exc.value.code == "missing_param"

    with pytest.raises(AnalysisError) as exc:
        service.run("describe", dataset["dataset_id"], {"top_n": 0})
    assert exc.value.code == "invalid_params"

    empty = tmp_path / "empty.csv"
    empty.write_text("a,b\n", encoding="utf-8")
    empty_ds = service.datasets.import_path(empty, name="empty")
    with pytest.raises(AnalysisError) as exc:
        service.run("describe", empty_ds["dataset_id"], {})
    assert exc.value.code == "empty_dataset"


def test_correlation_column_cap(tmp_path, tmp_db):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    settings.analysis.max_correlation_columns = 2
    datasets = DatasetService(tmp_db, settings)
    csv = tmp_path / "wide.csv"
    csv.write_text("a,b,c\n1,2,3\n2,3,4\n3,4,5\n", encoding="utf-8")
    dataset = datasets.import_path(csv, name="wide")
    service = AnalysisService(datasets, settings)
    result = service.run("correlation", dataset["dataset_id"], {})
    assert len(result["data"]["columns"]) == 2
    assert any("截断" in w for w in result["warnings"])
