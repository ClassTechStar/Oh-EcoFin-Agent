"""分析模块公共工具：列校验、类型判定、JSON 规整与可复现哈希。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend.analysis.models import AnalysisError

NUMERIC_TYPE = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|"
    r"UBIGINT|FLOAT|REAL|DOUBLE|DECIMAL)")
DATE_TYPE = re.compile(r"^(DATE|TIMESTAMP)")
STRING_TYPE = re.compile(r"^(VARCHAR|CHAR|TEXT|STRING|ENUM)")


def ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def escaped_path(path: str | Path) -> str:
    return str(path).replace("'", "''")


def parquet_relation(path: str | Path) -> str:
    return f"read_parquet('{escaped_path(path)}')"


def is_numeric(dtype: str) -> bool:
    return bool(NUMERIC_TYPE.match(str(dtype).upper()))


def is_date(dtype: str) -> bool:
    return bool(DATE_TYPE.match(str(dtype).upper()))


def is_string(dtype: str) -> bool:
    return bool(STRING_TYPE.match(str(dtype).upper()))


def ensure_columns(schema: dict[str, str], columns: list[str] | tuple[str, ...],
                   *, kind: str = "any") -> None:
    missing = [c for c in columns if c not in schema]
    if missing:
        raise AnalysisError(
            f"列不存在: {missing}", code="column_not_found",
            hint=f"可用列: {', '.join(schema)}")
    if kind == "numeric":
        bad = [c for c in columns if not is_numeric(schema[c])]
        if bad:
            raise AnalysisError(
                f"列不是数值类型: {bad}", code="column_type",
                hint="请选择数值列，或先用 SQL 模块做类型转换")
    if kind == "date":
        bad = [c for c in columns if not (is_date(schema[c]) or is_string(schema[c]))]
        if bad:
            raise AnalysisError(
                f"列不是日期类型: {bad}", code="column_type",
                hint="日期列需为 DATE/TIMESTAMP 或可解析的字符串")


def numeric_columns(schema: dict[str, str]) -> list[str]:
    return [name for name, dtype in schema.items() if is_numeric(dtype)]


def categorical_columns(schema: dict[str, str]) -> list[str]:
    return [name for name, dtype in schema.items()
            if not is_numeric(dtype) and not is_date(dtype)]


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()[:128]
    return value


def round_nested(value: Any, digits: int = 10) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    if isinstance(value, Decimal):
        return round(float(value), digits)
    if isinstance(value, dict):
        return {str(k): round_nested(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_nested(v, digits) for v in value]
    return value


def canonical_json(payload: Any) -> str:
    return json.dumps(round_nested(payload), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def table_spec(columns: list[str], rows: list[list[Any]]) -> dict:
    return {"columns": columns, "rows": rows}


def fmt_value(value: Any) -> Any:
    """展示层统一：浮点保留 6 位，None 保留。"""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    return json_value(value)
