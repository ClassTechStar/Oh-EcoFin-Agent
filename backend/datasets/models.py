"""数据集契约（M2）。

方案 6.6 DatasetManifest：dataset_id/version/sha256/format/rows/columns/
schema_json/source_uri/created_at。另补 name 与 table_name，便于 SQL 查询层
把数据集映射为稳定表名。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    dataset_id: str
    name: str
    version: int = 1
    sha256: str
    format: Literal["parquet"] = "parquet"
    rows: int
    columns: int
    #: 序列化为 `schema_json`（方案契约名）；字段名避开 BaseModel.schema 弃用属性
    schema_fields: dict[str, str] = Field(default_factory=dict, alias="schema_json")
    source_uri: str = ""
    table_name: str = ""
    created_at: str = ""


class DatasetError(ValueError):
    """数据集业务错误（可带可执行提示）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class DatasetLimitError(DatasetError):
    """超过导入限额（行/列/文件大小）。"""


class DatasetImportError(DatasetError):
    """无法解析或写入数据集。"""


class DatasetNotFoundError(DatasetError):
    """数据集或版本不存在。"""
