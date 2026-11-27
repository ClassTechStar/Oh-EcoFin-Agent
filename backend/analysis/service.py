"""分析服务（M4）：模块分派、参数校验、超时控制与可复现哈希。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
from pydantic import ValidationError

from backend.analysis.common import parquet_relation, payload_hash
from backend.analysis.models import AnalysisError
from backend.analysis.modules import MODULES, AnalysisContext, module_infos
from backend.config import Settings
from backend.datasets.service import DatasetService


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class AnalysisService:
    def __init__(self, datasets: DatasetService, settings: Settings):
        self.datasets = datasets
        self.settings = settings
        self.config = settings.analysis

    def modules(self) -> list[dict]:
        return module_infos()

    def run(self, module: str, dataset_id: str, params: dict | None = None,
            version: int | None = None) -> dict:
        spec = MODULES.get(module)
        if spec is None:
            raise AnalysisError(
                f"未知分析模块: {module}", code="module_not_found",
                hint=f"可用模块: {', '.join(MODULES)}")
        payload = self.datasets.store.get_version(dataset_id, version)
        if payload is None:
            raise AnalysisError(
                f"数据集不存在: {dataset_id}", code="dataset_not_found",
                hint="先调用 dataset_list 查看可用数据集")
        path = Path(payload["path"])
        if not path.is_file():
            raise AnalysisError(
                f"数据集快照文件缺失: {path}", code="dataset_not_found")
        try:
            validated = spec.params_model.model_validate(params or {})
        except ValidationError as e:
            raise AnalysisError(
                f"参数校验失败: {e.errors()[:3]}", code="invalid_params",
                hint="对照 /api/analysis/modules 的参数说明修正") from e

        started = time.perf_counter()
        conn = duckdb.connect()
        timer: threading.Timer | None = None
        try:
            relation = parquet_relation(path)
            schema = {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    f"DESCRIBE SELECT * FROM {relation}").fetchall()
            }
            timer = threading.Timer(self.config.timeout_seconds, conn.interrupt)
            timer.daemon = True
            timer.start()
            ctx = AnalysisContext(
                conn=conn, relation=relation, schema=schema, params=validated,
                max_describe_columns=self.config.max_describe_columns,
                max_correlation_columns=self.config.max_correlation_columns,
                max_trend_points=self.config.max_trend_points,
                max_groups=self.config.max_groups)
            outcome = spec.run(ctx)
        except duckdb.InterruptException as e:
            raise AnalysisError(
                f"分析超时（>{self.config.timeout_seconds}s）", code="timeout",
                hint="缩小数据范围或调大 FINAGENT_ANALYSIS__TIMEOUT_SECONDS") from e
        except duckdb.Error as e:
            raise AnalysisError(
                f"分析执行失败: {e}", code="execution_error",
                hint="检查列类型与参数后重试") from e
        finally:
            if timer is not None:
                timer.cancel()
            conn.close()

        data = outcome["data"]
        warnings = outcome.get("warnings") or []
        elapsed = int((time.perf_counter() - started) * 1000)
        manifest = payload.get("manifest") or {}
        result_payload = {
            "module": module,
            "params": validated.model_dump(),
            "dataset_sha256": payload["sha256"],
            "data": data,
        }
        result_hash = payload_hash(result_payload)
        chart_spec = None
        chart = outcome.get("chart")
        if chart:
            chart_payload = {
                "chart_type": chart["chart_type"],
                "title": chart["title"],
                "option": chart["option"],
                "dataset_id": dataset_id,
                "dataset_version": payload["version"],
                "dataset_sha256": payload["sha256"],
                "module": module,
                "result_hash": result_hash,
            }
            chart_spec = {
                **chart_payload,
                "spec_hash": payload_hash(chart_payload),
                "created_at": _now(),
            }
        return {
            "ok": True,
            "module": module,
            "label": spec.label,
            "dataset": {
                "dataset_id": dataset_id,
                "name": payload["name"],
                "version": payload["version"],
                "sha256": payload["sha256"],
                "table_name": payload["table_name"],
                "rows": manifest.get("rows", 0),
                "columns": manifest.get("columns", 0),
            },
            "params": validated.model_dump(),
            "data": data,
            "warnings": warnings,
            "result_hash": result_hash,
            "elapsed_ms": elapsed,
            "chart": chart_spec,
            "generated_at": _now(),
        }
