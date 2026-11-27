"""分析模块契约（M4）。

与 SQL 查询层一致：所有结果带 dataset 版本/哈希、参数、result_hash 与
chart.spec_hash，使同一输入重跑可复现；分析只读，不修改数据集。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnalysisError(ValueError):
    """分析业务错误（列不存在/类型不符/数据为空等）。"""

    def __init__(self, message: str, *, code: str = "analysis_error",
                 hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class AnalysisRunRequest(BaseModel):
    module: str = Field(..., min_length=1, max_length=32)
    dataset_id: str = Field(..., min_length=1, max_length=64)
    version: int | None = Field(None, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ChartSpec(BaseModel):
    chart_type: str
    title: str
    option: dict[str, Any]
    spec_hash: str = ""
    created_at: str = ""
