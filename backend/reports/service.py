"""报告服务（M5）：组合分析/SQL/文本块 → report spec → HTML/Word/Excel。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.analysis.common import payload_hash
from backend.analysis.models import AnalysisError
from backend.analysis.service import AnalysisService
from backend.audit.service import AuditService
from backend.config import Settings
from backend.engine.prompts import DISCLAIMER
from backend.query.engine import QueryEngine, QueryExecutionError
from backend.query.guard import SqlGuardError
from backend.reports.models import (
    ReportAuditError,
    ReportComposeRequest,
    ReportError,
)
from backend.reports.store import ReportStore

MEDIA_TYPES = {
    "html": "text/html; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _safe_filename(text: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|]+', "_", text).strip()
    return clean[:80] or "report"


class ReportService:
    def __init__(self, db, settings: Settings, analysis: AnalysisService,
                 query: QueryEngine, audit: AuditService | None = None):
        self.settings = settings
        self.analysis = analysis
        self.query = query
        self.store = ReportStore(db)
        self.audit = audit or AuditService(db, settings)
        self.report_dir = Path(settings.reports.dir)

    # ---------- 组合 ----------

    def compose(self, request: ReportComposeRequest) -> dict:
        max_blocks = int(self.settings.reports.max_blocks)
        if len(request.blocks) > max_blocks:
            raise ReportError(
                f"报告块数超过上限: {len(request.blocks)} > {max_blocks}",
                code="too_many_blocks")
        report_id = f"rpt_{uuid.uuid4().hex[:12]}"
        created_at = _now()
        blocks: list[dict] = []
        sources: list[dict] = []
        datasets: dict[str, dict] = {}

        for index, block in enumerate(request.blocks, start=1):
            block_id = f"b{index}"
            if block.type == "text":
                title = block.title or "说明"
                source = {
                    "type": "narrative", "result_hash": "", "chart_spec_hash": "",
                    "sql_hash": "", "dataset_name": "", "dataset_version": None,
                    "dataset_sha256": "", "dataset_id": "",
                }
                blocks.append({
                    "id": block_id, "type": "text", "title": title,
                    "text": block.text, "source": {**source, "block_id": block_id,
                                                   "block_title": title},
                    "warnings": [], "chart": None, "chart_image": "",
                })
                sources.append(blocks[-1]["source"])
                continue

            if block.type == "analysis":
                try:
                    result = self.analysis.run(
                        block.module, block.dataset_id, block.params, block.version)
                except AnalysisError as e:
                    raise ReportError(
                        f"分析块 {block_id} 失败: {e}", code=e.code,
                        hint=e.hint) from e
                title = block.title or result["label"]
                chart = result.get("chart")
                if chart:
                    # 图表规格的 created_at 不参与报告复现哈希
                    chart = {key: value for key, value in chart.items()
                             if key != "created_at"}
                source = {
                    "type": "analysis",
                    "dataset_id": result["dataset"]["dataset_id"],
                    "dataset_name": result["dataset"]["name"],
                    "dataset_version": result["dataset"]["version"],
                    "dataset_sha256": result["dataset"]["sha256"],
                    "module": result["module"],
                    "params": result["params"],
                    "result_hash": result["result_hash"],
                    "chart_spec_hash": (chart or {}).get("spec_hash", ""),
                    "sql_hash": "",
                    "block_id": block_id, "block_title": title,
                }
                datasets[result["dataset"]["dataset_id"]] = {
                    key: result["dataset"].get(key)
                    for key in ("dataset_id", "name", "version", "sha256",
                                "table_name", "rows", "columns")
                }
                blocks.append({
                    "id": block_id, "type": "analysis", "title": title,
                    "module": result["module"], "label": result["label"],
                    "params": result["params"], "data": result["data"],
                    "chart": chart,
                    "chart_image": f"charts/{block_id}.png" if chart else "",
                    "warnings": result.get("warnings") or [],
                    "source": source,
                })
                sources.append(source)
                continue

            # SQL 块
            try:
                result = self.query.execute(
                    block.sql, max_rows=block.limit,
                    dataset_ids=[block.dataset_id] if block.dataset_id else None)
            except (SqlGuardError, QueryExecutionError) as e:
                raise ReportError(
                    f"SQL 块 {block_id} 失败: {e}",
                    code=getattr(e, "code", "execution_error"),
                    hint=getattr(e, "hint", "")) from e
            title = block.title or "SQL 查询"
            first = self._dataset_meta(result["datasets"][0]) if result["datasets"] else {}
            if first:
                datasets[first["dataset_id"]] = first
            source = {
                "type": "sql", "sql": block.sql,
                "sql_hash": result["sql_hash"],
                "result_hash": result["result_hash"],
                "dataset_id": first.get("dataset_id", ""),
                "dataset_name": first.get("name", ""),
                "dataset_version": first.get("version"),
                "dataset_sha256": first.get("sha256", ""),
                "chart_spec_hash": "",
                "datasets": result["datasets"],
                "block_id": block_id, "block_title": title,
            }
            blocks.append({
                "id": block_id, "type": "sql", "title": title,
                "sql": block.sql, "columns": result["columns"],
                "rows": result["rows"], "row_count": result["row_count"],
                "truncated": result["truncated"], "warnings": [],
                "chart": None, "chart_image": "", "source": source,
            })
            sources.append(source)

        spec = {
            "report_id": report_id,
            "title": request.title,
            "subtitle": request.subtitle,
            "status": "auditing",
            "created_at": created_at,
            "request": request.model_dump(mode="json"),
            "blocks": blocks,
            "sources": sources,
            "datasets": sorted(datasets.values(), key=lambda d: str(d.get("name"))),
            "max_table_rows": int(self.settings.reports.max_table_rows),
            "disclaimer": DISCLAIMER,
        }
        spec["spec_hash"] = payload_hash({
            "title": spec["title"],
            "subtitle": spec["subtitle"],
            "blocks": spec["blocks"],
            "sources": spec["sources"],
            "datasets": spec["datasets"],
            "max_table_rows": spec["max_table_rows"],
            "disclaimer": spec["disclaimer"],
        })
        spec["manifest"] = self._build_manifest(spec)

        try:
            audit = self.audit.audit_report(spec)
        except Exception as e:
            raise ReportError(
                f"报告审计失败: {type(e).__name__}: {e}", code="audit_error",
                hint="检查证据构建与数字解析") from e
        spec["audit"] = audit
        status = "ready" if audit["status"] == "passed" else "blocked"
        spec["status"] = status

        report_dir = self.report_dir / report_id
        report_dir.mkdir(parents=True, exist_ok=True)
        if status == "blocked":
            (report_dir / "spec.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            self.store.save(report_id, spec["title"], spec["subtitle"],
                            "blocked", spec)
            raise ReportAuditError(
                f"报告数字未通过证据审计：{audit['claims_failed']} 个数字缺证据",
                report_id, audit)

        # 渲染依赖（matplotlib/python-docx）按需加载，避免拖慢服务冷启动
        from backend.reports.charts import render_chart_png
        from backend.reports.docx_report import render_docx
        from backend.reports.html_report import render_html
        from backend.reports.xlsx_report import render_xlsx

        try:
            for block in blocks:
                if block.get("chart") and block.get("chart_image"):
                    render_chart_png(
                        block["chart"], report_dir / block["chart_image"],
                        dpi=int(self.settings.reports.chart_dpi))
            html_path = render_html(spec, report_dir)
            docx_path = render_docx(spec, report_dir)
            xlsx_path = render_xlsx(spec, report_dir)
        except Exception as e:
            shutil.rmtree(report_dir, ignore_errors=True)
            raise ReportError(
                f"报告渲染失败: {type(e).__name__}: {e}", code="render_error",
                hint="检查图表 spec 与数据表后重试") from e

        files = []
        for fmt, path in (("html", html_path), ("docx", docx_path),
                          ("xlsx", xlsx_path)):
            digest = _sha256_file(path)
            files.append({
                "format": fmt, "filename": path.name,
                "sha256": digest, "bytes": path.stat().st_size,
            })
            self.store.add_file(report_id, fmt, path.name, str(path), digest,
                                path.stat().st_size)
        spec["files"] = files
        (report_dir / "spec.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        self.store.save(report_id, spec["title"], spec["subtitle"], status, spec)
        return spec

    @staticmethod
    def _build_manifest(spec: dict) -> dict:
        return {
            "report_id": spec["report_id"],
            "spec_hash": spec["spec_hash"],
            "generated_at": spec["created_at"],
            "software": "oh-ecofin-agent/4.0.0",
            "datasets": [{
                "dataset_id": dataset.get("dataset_id"),
                "name": dataset.get("name"),
                "version": dataset.get("version"),
                "sha256": dataset.get("sha256"),
            } for dataset in spec.get("datasets") or []],
            "blocks": [{
                "id": block.get("id"),
                "type": block.get("type"),
                "title": block.get("title"),
                "result_hash": (block.get("source") or {}).get("result_hash", ""),
                "chart_spec_hash": (block.get("source") or {}).get("chart_spec_hash", ""),
                "sql_hash": (block.get("source") or {}).get("sql_hash", ""),
                "dataset_sha256": (block.get("source") or {}).get("dataset_sha256", ""),
            } for block in spec.get("blocks") or []],
        }

    def rerun(self, report_id: str) -> dict:
        payload = self.store.get(report_id)
        if payload is None:
            raise ReportError(f"报告不存在: {report_id}", code="report_not_found")
        old_spec = payload.get("spec") or {}
        request_data = old_spec.get("request")
        if not request_data:
            raise ReportError(
                "报告缺少原始请求，无法重跑", code="no_request",
                hint="M5 之前生成的报告不包含 request manifest")
        request = ReportComposeRequest.model_validate(request_data)
        try:
            new_spec = self.compose(request)
        except ReportAuditError as e:
            return {
                "same": False, "blocked": True,
                "old_report_id": report_id, "report_id": e.report_id,
                "audit": e.audit,
                "differences": ["重跑报告仍被审计阻断"],
            }
        differences = self._compare_specs(old_spec, new_spec)
        return {
            "same": not differences,
            "blocked": False,
            "old_report_id": report_id,
            "report_id": new_spec["report_id"],
            "old_spec_hash": old_spec.get("spec_hash", ""),
            "new_spec_hash": new_spec.get("spec_hash", ""),
            "differences": differences,
        }

    @staticmethod
    def _compare_specs(old: dict, new: dict) -> list[str]:
        differences: list[str] = []
        if old.get("spec_hash") != new.get("spec_hash"):
            differences.append("spec_hash 不一致")
        old_datasets = {d.get("dataset_id"): d for d in old.get("datasets") or []}
        new_datasets = {d.get("dataset_id"): d for d in new.get("datasets") or []}
        for dataset_id in sorted(set(old_datasets) | set(new_datasets)):
            old_dataset = old_datasets.get(dataset_id) or {}
            new_dataset = new_datasets.get(dataset_id) or {}
            if old_dataset.get("version") != new_dataset.get("version"):
                differences.append(f"数据集 {dataset_id} 版本不一致")
            if old_dataset.get("sha256") != new_dataset.get("sha256"):
                differences.append(f"数据集 {dataset_id} sha256 不一致")
        old_blocks = {b.get("id"): b for b in old.get("blocks") or []}
        new_blocks = {b.get("id"): b for b in new.get("blocks") or []}
        for block_id in sorted(set(old_blocks) | set(new_blocks)):
            old_source = (old_blocks.get(block_id) or {}).get("source") or {}
            new_source = (new_blocks.get(block_id) or {}).get("source") or {}
            for key in ("result_hash", "chart_spec_hash", "sql_hash"):
                if old_source.get(key, "") != new_source.get(key, ""):
                    differences.append(f"块 {block_id} {key} 不一致")
        return differences

    def _dataset_meta(self, dataset_id: str) -> dict:
        payload = self.analysis.datasets.store.get_version(dataset_id)
        if not payload:
            return {"dataset_id": dataset_id}
        manifest = payload.get("manifest") or {}
        return {
            "dataset_id": dataset_id,
            "name": payload["name"],
            "version": payload["version"],
            "sha256": payload["sha256"],
            "table_name": payload["table_name"],
            "rows": manifest.get("rows", 0),
            "columns": manifest.get("columns", 0),
        }

    # ---------- 查询/导出 ----------

    def get(self, report_id: str) -> dict:
        payload = self.store.get(report_id)
        if payload is None:
            raise ReportError(f"报告不存在: {report_id}", code="report_not_found")
        spec = payload.get("spec") or {}
        spec = {**spec, "files": [
            {key: f[key] for key in ("format", "filename", "sha256", "bytes")}
            for f in payload.get("files") or []
        ]}
        return spec

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.store.list(limit)
        for row in rows:
            row["files"] = [
                {key: f[key] for key in ("format", "filename", "sha256", "bytes")}
                for f in row.get("files") or []
            ]
        return rows

    def export_path(self, report_id: str, fmt: str) -> tuple[Path, str]:
        payload = self.store.get(report_id)
        if payload is None:
            raise ReportError(f"报告不存在: {report_id}", code="report_not_found")
        row = next((f for f in payload["files"] if f["format"] == fmt), None)
        if row is None:
            raise ReportError(f"报告没有 {fmt} 导出", code="export_not_found")
        path = Path(row["path"])
        if not path.is_file():
            raise ReportError(f"报告文件缺失: {path}", code="export_not_found")
        if not path.resolve().is_relative_to(self.report_dir.resolve()):
            raise ReportError("报告文件路径非法", code="export_not_found")
        filename = f"{_safe_filename(payload['title'])}.{fmt}"
        return path, filename
