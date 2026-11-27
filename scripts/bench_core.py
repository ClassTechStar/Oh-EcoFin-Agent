"""核心接口 P95 基准（M9）。

在临时目录生成 5 万行数据集与 2 万条目录记录，测量目录检索、SQL、画像、
分析与报告组合的 P50/P95；默认 P95 上限 2000ms。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

from backend.analysis.service import AnalysisService  # noqa: E402
from backend.catalog.models import CatalogQuery  # noqa: E402
from backend.catalog.store import CatalogStore  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.datasets.service import DatasetService  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.query.engine import QueryAuditStore, QueryEngine  # noqa: E402
from backend.reports.models import ReportComposeRequest  # noqa: E402
from backend.reports.service import ReportService  # noqa: E402


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def _measure(fn, rounds: int = 10) -> dict:
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "p50_ms": round(ordered[len(ordered) // 2], 2),
        "p95_ms": round(_p95(samples), 2),
        "max_ms": round(max(samples), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="核心接口 P95 基准")
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--limit-ms", type=float, default=2000.0)
    args = parser.parse_args()

    temp = Path(tempfile.mkdtemp(prefix="oh-ecofin-bench-"))
    settings = Settings(db_path=str(temp / "bench.db"))
    settings.datasets.dir = str(temp / "datasets")
    settings.datasets.workspace_dir = str(temp / "workspaces")
    settings.reports.dir = str(temp / "reports")
    db = Database(settings.db_path)
    datasets = DatasetService(db, settings)
    catalog = CatalogStore(db)

    csv_path = temp / "bench.csv"
    rows = ["date,region,amount,qty"]
    regions = ("North", "South", "East", "West")
    for i in range(args.rows):
        rows.append(f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d},"
                    f"{regions[i % 4]},{i * 0.5 % 1000},{i % 17}")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    dataset = datasets.import_path(csv_path, name="bench_sales")

    def catalog_records():
        for i in range(20_000):
            yield {
                "instrument_id": f"US:BENCH{i}", "symbol": f"BENCH{i}",
                "exchange": "NMS", "asset_class": "equity",
                "name": f"Bench Company {i}", "country": "United States",
                "industry": "Software" if i % 2 else "Banks",
            }

    catalog.import_records(catalog_records(), source="bench",
                           source_version="v1", sha256="bench" * 12)
    analysis = AnalysisService(datasets, settings)
    query = QueryEngine(datasets, QueryAuditStore(db),
                        settings.datasets.workspace_dir,
                        max_rows=1000, timeout_seconds=30)
    reports = ReportService(db, settings, analysis, query)

    metrics = {
        "catalog_search": _measure(
            lambda: catalog.search(CatalogQuery(query="Bench", limit=50)),
            args.rounds),
        "dataset_profile": _measure(
            lambda: datasets.profile(dataset["dataset_id"]), max(3, args.rounds // 2)),
        "sql_query": _measure(
            lambda: query.execute(
                f"SELECT region, SUM(amount) AS total FROM {dataset['table_name']}"
                " GROUP BY region ORDER BY total DESC"), args.rounds),
        "analysis_group": _measure(
            lambda: analysis.run("group", dataset["dataset_id"], {
                "group_column": "region", "value_column": "amount", "agg": "sum"}),
            args.rounds),
        "report_compose": _measure(
            lambda: reports.compose(ReportComposeRequest.model_validate({
                "title": "Benchmark",
                "blocks": [{"type": "analysis", "dataset_id": dataset["dataset_id"],
                           "module": "group",
                           "params": {"group_column": "region",
                                      "value_column": "amount", "agg": "sum"}}],
            })), max(2, args.rounds // 3)),
    }
    failed = {name: value for name, value in metrics.items()
              if value["p95_ms"] > args.limit_ms}
    result = {
        "ok": not failed,
        "rows": args.rows,
        "p95_limit_ms": args.limit_ms,
        "metrics": metrics,
        "failed": failed,
        "temp_dir": str(temp),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
