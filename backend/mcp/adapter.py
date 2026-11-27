"""MCP 工具适配（M9）。

只暴露只读工具：catalog_search / dataset_list / dataset_profile /
analysis_modules / analysis_run / sql_query。报告写入与交易类副作用工具
不通过 MCP 暴露，避免绕过 Agent 的显式授权策略。
"""

from __future__ import annotations

import json

from backend.analysis.service import AnalysisService
from backend.audit.service import AuditService
from backend.catalog.store import CatalogStore
from backend.config import Settings
from backend.datasets.service import DatasetService
from backend.db import Database
from backend.query.engine import QueryAuditStore, QueryEngine
from backend.reports.service import ReportService
from backend.tools.data import (
    AnalysisRunParams,
    CatalogSearchParams,
    DatasetProfileParams,
    DataTools,
    SqlQueryParams,
)

READONLY_TOOLS = (
    "catalog_search", "dataset_list", "dataset_profile",
    "analysis_modules", "analysis_run", "sql_query",
)


class McpAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        db = Database(settings.db_path)
        catalog = CatalogStore(db)
        datasets = DatasetService(db, settings)
        query = QueryEngine(
            datasets, QueryAuditStore(db), settings.datasets.workspace_dir,
            max_rows=settings.datasets.query_max_rows,
            timeout_seconds=settings.datasets.query_timeout_seconds)
        analysis = AnalysisService(datasets, settings)
        audit = AuditService(db, settings)
        reports = ReportService(db, settings, analysis, query, audit=audit)
        self.tools = DataTools(catalog, datasets, query, analysis, reports)

    def list_tools(self) -> list[dict]:
        handlers = {
            "catalog_search": self.tools.catalog_search,
            "dataset_list": self.tools.dataset_list,
            "dataset_profile": self.tools.dataset_profile,
            "analysis_modules": self.tools.analysis_modules,
            "analysis_run": self.tools.analysis_run,
            "sql_query": self.tools.sql_query,
        }
        descriptions = {
            "catalog_search": "检索标的目录（代码/名称/行业/交易所），不返回行情。",
            "dataset_list": "列出已导入数据集与 SQL 表名。",
            "dataset_profile": "查看数据集列类型、缺失、唯一值与分位。",
            "analysis_modules": "列出内置分析模块与参数说明。",
            "analysis_run": "运行确定性分析模块，返回表格、result_hash 与 chart spec。",
            "sql_query": "对已登记数据集执行单条只读 SELECT/WITH。",
        }
        schemas = {
            "catalog_search": self._schema(CatalogSearchParams),
            "dataset_list": {"type": "object", "properties": {}},
            "dataset_profile": self._schema(DatasetProfileParams),
            "analysis_modules": {"type": "object", "properties": {}},
            "analysis_run": self._schema(AnalysisRunParams),
            "sql_query": self._schema(SqlQueryParams),
        }
        assert all(name in handlers for name in READONLY_TOOLS)
        return [{
            "name": name,
            "description": descriptions[name],
            "inputSchema": schemas[name],
        } for name in READONLY_TOOLS]

    @staticmethod
    def _schema(model) -> dict:
        schema = model.model_json_schema()
        schema.pop("title", None)
        return schema

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        if name not in READONLY_TOOLS:
            raise ValueError(f"MCP 未暴露工具: {name}")
        handler = getattr(self.tools, name)
        return await handler(**(arguments or {}))

    @staticmethod
    def text_result(result: dict) -> dict:
        return {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": not result.get("ok", True),
        }
