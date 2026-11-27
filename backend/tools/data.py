"""数据工具面（M1-M3）：标的目录检索、数据集画像、只读 SQL。

全部工具无副作用的读操作；SQL 只能访问已登记数据集表，且受 SQL Guard、
只读连接、行数/超时限制三重约束。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.analysis.service import AnalysisService
from backend.catalog.models import CatalogQuery
from backend.catalog.store import CatalogStore
from backend.datasets.service import DatasetService
from backend.query.engine import QueryEngine
from backend.reports.models import (
    ReportAuditError,
    ReportComposeRequest,
    ReportError,
)
from backend.reports.service import ReportService


class CatalogSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field("", max_length=100,
                       description="代码/名称/行业关键词，可留空")
    symbol: str = Field("", max_length=32, description="精确代码片段，如 600519")
    asset_class: str = Field("", max_length=20,
                             description="equity/etf/fund/index/fx/crypto/moneymarket")
    exchange: str = Field("", max_length=16, description="交易所代码，如 SHH/HKG/NMS")
    country: str = Field("", max_length=80, description="国家/地区，如 China")
    industry: str = Field("", max_length=80, description="行业关键词，如 Banks")
    listing_status: str = Field("active", max_length=16,
                               description="active/delisted/unknown/all")
    limit: int = Field(20, ge=1, le=100, description="返回条数上限")


class DatasetListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetProfileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(..., min_length=1, max_length=64,
                            description="dataset_id，来自 dataset_list")


class SqlQueryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(..., min_length=1, max_length=20000,
                     description="单条 SELECT/WITH 查询，表名来自 dataset_list")
    limit: int = Field(100, ge=1, le=500,
                       description="返回行数上限（防止上下文过载）")


class AnalysisModulesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisRunParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str = Field(..., min_length=1, max_length=32,
                        description="describe/missing/correlation/trend/group/funnel/ab_test")
    dataset_id: str = Field(..., min_length=1, max_length=64,
                            description="dataset_id，来自 dataset_list")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="模块参数；先用 analysis_modules 查看字段说明")


class ReportComposeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field("数据分析报告", min_length=1, max_length=120)
    subtitle: str = Field("", max_length=200)
    blocks: list[dict[str, Any]] = Field(
        ..., min_length=1, max_length=30,
        description="有序块：analysis(module/dataset_id/params) / sql(sql) / text(text)")


class SandboxRunParams(BaseModel):
    """自由代码沙箱工具参数（仅 FINAGENT_SANDBOX__ENABLED=1 时注册）。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        ..., min_length=1, max_length=20000,
        description="Python 代码；只能使用 datasets/pd/np/save_output，禁止文件与网络")
    dataset_ids: list[str] = Field(
        default_factory=list, max_length=5,
        description="要加载的 dataset_id 列表；留空则加载最新数据集（最多 5 个）")


class DataTools:
    def __init__(self, catalog: CatalogStore, datasets: DatasetService,
                 query: QueryEngine, analysis: AnalysisService,
                 reports: ReportService):
        self.catalog = catalog
        self.datasets = datasets
        self.query = query
        self.analysis = analysis
        self.reports = reports

    async def catalog_search(self, query: str = "", symbol: str = "",
                             asset_class: str = "", exchange: str = "",
                             country: str = "", industry: str = "",
                             listing_status: str = "active", limit: int = 20) -> dict:
        params = CatalogQuery(
            query=query, symbol=symbol, asset_class=asset_class,
            exchange=exchange, country=country, industry=industry,
            listing_status=listing_status, limit=limit)
        result = await asyncio.to_thread(self.catalog.search, params)
        return {
            **result,
            "hint": "instrument_id 可作为后续分析/报告的标的引用；"
                    "目录不含行情，行情请用 get_quote/get_kline。",
        }

    async def dataset_list(self) -> dict:
        items = await asyncio.to_thread(self.datasets.list)
        return {
            "datasets": items,
            "count": len(items),
            "hint": "SQL 查询使用 table_name；先 dataset_profile 了解列与缺失情况。",
        }

    async def dataset_profile(self, dataset_id: str) -> dict:
        result = await asyncio.to_thread(self.datasets.profile, dataset_id)
        return result

    async def sql_query(self, sql: str, limit: int = 100) -> dict:
        result = await asyncio.to_thread(self.query.execute, sql, max_rows=limit)
        columns = result.pop("columns")
        result["columns"] = columns
        result["rows"] = [
            dict(zip(columns, row, strict=False)) for row in result["rows"]
        ]
        result["hint"] = "报告引用数字时必须标注 result_hash 与 datasets，便于证据回放。"
        return result

    async def analysis_modules(self) -> dict:
        modules = await asyncio.to_thread(self.analysis.modules)
        return {"modules": modules, "count": len(modules),
                "hint": "按模块 params 说明传参；列名先用 dataset_profile 核对。"}

    async def analysis_run(self, module: str, dataset_id: str,
                           params: dict[str, Any] | None = None) -> dict:
        result = await asyncio.to_thread(
            self.analysis.run, module, dataset_id, params or {})
        result["hint"] = ("chart 为可直接渲染的 ECharts option；"
                          "引用数字时保留 result_hash 与 chart.spec_hash。")
        return result

    async def report_compose(self, title: str = "数据分析报告",
                             subtitle: str = "",
                             blocks: list[dict[str, Any]] | None = None) -> dict:
        try:
            request = ReportComposeRequest.model_validate({
                "title": title, "subtitle": subtitle, "blocks": blocks or [],
            })
        except ValidationError as e:
            raise ReportError(
                f"报告参数校验失败: {e.errors()[:3]}", code="invalid_params",
                hint="blocks 每项需含 type=analysis/sql/text 及对应字段") from e
        try:
            result = await asyncio.to_thread(self.reports.compose, request)
        except ReportAuditError as e:
            return {
                "ok": False, "error": str(e), "code": e.code,
                "hint": e.hint, "report_id": e.report_id, "audit": e.audit,
            }
        result["hint"] = "HTML/Word/Excel 已落盘；报告数字均带数据集 sha 与 result_hash。"
        return result
