"""报告契约（M5）。
报告由有序 block 组成：分析块、SQL 块、叙述文本块。分析/SQL 块在服务端
重新执行并记录数据版本与哈希，确保报告数字可回溯到数据集快照与工具结果。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ReportError(ValueError):
    def __init__(self, message: str, *, code: str = "report_error",
                 hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class ReportAuditError(ReportError):
    """报告数字/证据未通过质量闸门（报告已保存为 blocked，供审计页查看）。"""

    def __init__(self, message: str, report_id: str, audit: dict):
        super().__init__(message, code="audit_blocked",
                         hint="查看审计页/GET /api/audit/reports/{report_id}")
        self.report_id = report_id
        self.audit = audit


class ReportAnalysisBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["analysis"] = "analysis"
    title: str = Field("", max_length=120)
    dataset_id: str = Field(..., min_length=1, max_length=64)
    module: str = Field(..., min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(None, ge=1)


class ReportSqlBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sql"] = "sql"
    title: str = Field("", max_length=120)
    dataset_id: str = Field("", max_length=64)
    sql: str = Field(..., min_length=1, max_length=20000)
    limit: int = Field(200, ge=1, le=2000)


class ReportTextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    title: str = Field("", max_length=120)
    text: str = Field(..., min_length=1, max_length=20000)


ReportBlock = Annotated[
    Union[ReportAnalysisBlock, ReportSqlBlock, ReportTextBlock],
    Field(discriminator="type"),
]


class ReportComposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field("数据分析报告", min_length=1, max_length=120)
    subtitle: str = Field("", max_length=200)
    blocks: list[ReportBlock] = Field(..., min_length=1, max_length=30)


class ReportFileInfo(BaseModel):
    format: Literal["html", "docx", "xlsx"]
    filename: str
    sha256: str
    bytes: int
