"""报告证据账本构建（M7）。

分析/SQL 表格的每个数值单元格、分析结果中的数值标量、图表 spec 都登记为
evidence：带来源、locator、excerpt、value_hash 与可匹配的 value_num。
"""

from __future__ import annotations

import hashlib
from typing import Any

from backend.analysis.common import canonical_json
from backend.audit.numbers import parse_cell_number


def _value_hash(source: str, locator: str, value: Any) -> str:
    payload = canonical_json({"source": source, "locator": locator, "value": value})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _evidence_id(report_id: str, block_id: str, locator: str, value_hash: str) -> str:
    raw = f"{report_id}|{block_id}|{locator}|{value_hash}"
    return "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _make_evidence(report_id: str, block_id: str, source: str, locator: str,
                   excerpt: str, value_num: float | None, value_text: str) -> dict:
    value_hash = _value_hash(source, locator, value_text)
    return {
        "id": _evidence_id(report_id, block_id, locator, value_hash),
        "report_id": report_id,
        "run_id": report_id,
        "tool_call_id": block_id,
        "block_id": block_id,
        "source": source,
        "locator": locator,
        "excerpt": excerpt[:500],
        "value_hash": value_hash,
        "value_num": value_num,
        "value_text": value_text[:500],
    }


def _walk_scalars(data: Any, path: str, max_depth: int = 4):
    if max_depth < 0:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("table", "x", "series", "matrix", "items", "columns"):
                continue
            yield from _walk_scalars(value, f"{path}.{key}", max_depth - 1)
    elif isinstance(data, list):
        for index, value in enumerate(data[:200]):
            yield from _walk_scalars(value, f"{path}[{index}]", max_depth - 1)
    else:
        yield path, data


def build_evidence(spec: dict, max_evidence: int) -> tuple[list[dict], bool]:
    report_id = spec["report_id"]
    evidence: list[dict] = []
    truncated = False

    def add(item: dict) -> bool:
        nonlocal truncated
        if len(evidence) >= max_evidence:
            truncated = True
            return False
        evidence.append(item)
        return True

    for block in spec.get("blocks") or []:
        block_id = block.get("id", "")
        block_type = block.get("type")
        if block_type == "analysis":
            chart = block.get("chart") or {}
            if chart.get("spec_hash"):
                add(_make_evidence(
                    report_id, block_id, "chart",
                    f"{block_id}/chart",
                    f"{block.get('title', '')} chart_spec={chart['spec_hash']}",
                    None, str(chart["spec_hash"])))
            table = (block.get("data") or {}).get("table") or {}
            columns = table.get("columns") or []
            rows = table.get("rows") or []
            source = f"analysis:{block.get('module', '')}"
            for row_index, row in enumerate(rows):
                for col_index, value in enumerate(row):
                    parsed = parse_cell_number(value)
                    if parsed is None:
                        continue
                    number, _unit = parsed
                    column = str(columns[col_index]) if col_index < len(columns) else f"c{col_index}"
                    locator = f"{block_id}/table/r{row_index}/{column}"
                    if not add(_make_evidence(
                            report_id, block_id, source, locator,
                            f"{column}={value}", number, str(value))):
                        break
                if truncated:
                    break
            if not truncated:
                for path, value in _walk_scalars(block.get("data") or {}, f"{block_id}/data"):
                    parsed = parse_cell_number(value)
                    if parsed is None:
                        continue
                    number, _unit = parsed
                    if not add(_make_evidence(
                            report_id, block_id, f"analysis:{block.get('module', '')}",
                            path, f"{path}={value}", number, str(value))):
                        break
        elif block_type == "sql":
            columns = block.get("columns") or []
            rows = block.get("rows") or []
            for row_index, row in enumerate(rows):
                for col_index, value in enumerate(row):
                    parsed = parse_cell_number(value)
                    if parsed is None:
                        continue
                    number, _unit = parsed
                    column = str(columns[col_index]) if col_index < len(columns) else f"c{col_index}"
                    locator = f"{block_id}/table/r{row_index}/{column}"
                    if not add(_make_evidence(
                            report_id, block_id, "sql", locator,
                            f"{column}={value}", number, str(value))):
                        break
                if truncated:
                    break
        if truncated:
            break
    return evidence, truncated
