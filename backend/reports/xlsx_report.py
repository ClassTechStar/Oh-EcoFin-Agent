"""Excel 报告（M5）：openpyxl 数据表 + 原生图表 + 条件格式。

Excel 用原生图表而非图片，便于二次编辑；heatmap 用色阶条件格式，
funnel 用横向条形图表达。
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="F4F6F8")
HEADER_FONT = Font(bold=True, size=10, color="44515D")
BODY_FONT = Font(size=9)
MUTED_FONT = Font(size=8, color="7A8590")
THIN = Side(style="thin", color="DFE4EA")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _sheet_title(name: str, used: set[str], fallback: str) -> str:
    clean = re.sub(r"[\[\]:*?/\\]", " ", str(name or "")).strip() or fallback
    clean = clean[:28]
    title = clean
    index = 2
    while title in used:
        title = f"{clean[:26]}_{index}"
        index += 1
    used.add(title)
    return title


def _write_table(ws, start_row: int, columns: list, rows: list,
                 limit: int, *, title: str = "") -> int:
    row = start_row
    if title:
        ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=11)
        row += 1
    for idx, name in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=idx, value=str(name))
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    row += 1
    for data_row in rows[:limit]:
        for idx, value in enumerate(data_row, start=1):
            cell = ws.cell(row=row, column=idx,
                           value="—" if value is None else value)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        row += 1
    for idx, name in enumerate(columns, start=1):
        width = max(10, min(30, len(str(name)) + 6))
        for data_row in rows[:min(limit, 30)]:
            if idx - 1 < len(data_row):
                width = max(width, min(34, len(str(data_row[idx - 1])) + 2))
        ws.column_dimensions[get_column_letter(idx)].width = width
    if rows:
        ws.freeze_panes = ws.cell(row=start_row + (1 if title else 0) + 1,
                                  column=1)
    return row


def _chart_data(option: dict) -> tuple[list[str], list[str], list[list]] | None:
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    if series[0].get("type") in ("pie", "funnel"):
        data = series[0].get("data") or []
        names = [str(d.get("name", "")) for d in data]
        values = [d.get("value") for d in data]
        return names, ["值"], [values]
    axis = option.get("xAxis") or {}
    categories = [str(x) for x in axis.get("data") or []]
    names = [str(s.get("name") or f"系列{i + 1}") for i, s in enumerate(series)]
    values = []
    for item in series:
        row = list(item.get("data") or [])
        values.append(row + [None] * (len(categories) - len(row)))
    if not categories:
        return None
    return categories, names, values


def _add_chart(ws, block: dict, start_row: int) -> None:
    option = (block.get("chart") or {}).get("option") or {}
    chart_type = (block.get("chart") or {}).get("chart_type") or "bar"
    data = _chart_data(option)
    if not data:
        return
    categories, names, values = data
    row = start_row
    ws.cell(row=row, column=1, value="类别" if chart_type != "pie" else "名称").font = HEADER_FONT
    for idx, name in enumerate(names, start=2):
        ws.cell(row=row, column=idx, value=name).font = HEADER_FONT
    for r_idx, category in enumerate(categories, start=1):
        ws.cell(row=row + r_idx, column=1, value=category).font = BODY_FONT
        for s_idx, series_values in enumerate(values, start=2):
            value = series_values[r_idx - 1] if r_idx - 1 < len(series_values) else None
            ws.cell(row=row + r_idx, column=s_idx, value=value).font = BODY_FONT
    data_ref = Reference(ws, min_col=2, max_col=1 + len(names),
                         min_row=row, max_row=row + len(categories))
    cats_ref = Reference(ws, min_col=1, min_row=row + 1,
                         max_row=row + len(categories))
    title = block.get("title") or ""
    if chart_type == "line":
        chart = LineChart()
    elif chart_type == "pie":
        chart = PieChart()
    else:
        chart = BarChart()
        chart.type = "bar" if chart_type == "funnel" else "col"
    chart.title = title
    chart.height = 9
    chart.width = 16
    chart.add_data(data_ref, titles_from_data=True)
    if chart_type != "pie":
        chart.set_categories(cats_ref)
        chart.y_axis.title = str((option.get("yAxis") or {}).get("name") or "")
    else:
        chart.set_categories(cats_ref)
    ws.add_chart(chart, f"H{max(3, row - 1)}")


def _write_heatmap(ws, block: dict, start_row: int) -> int:
    option = (block.get("chart") or {}).get("option") or {}
    x_labels = [str(x) for x in (option.get("xAxis") or {}).get("data") or []]
    y_labels = [str(x) for x in (option.get("yAxis") or {}).get("data") or []]
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    pairs = (series[0].get("data") if series else None) or []
    if not x_labels or not y_labels:
        return start_row
    ws.cell(row=start_row, column=1, value="相关系数矩阵").font = Font(bold=True, size=11)
    header_row = start_row + 1
    ws.cell(row=header_row, column=1, value="字段").font = HEADER_FONT
    for idx, label in enumerate(x_labels, start=2):
        ws.cell(row=header_row, column=idx, value=label).font = HEADER_FONT
    matrix = {label: {} for label in y_labels}
    for pair in pairs:
        try:
            x_idx, y_idx, value = int(pair[0]), int(pair[1]), float(pair[2])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= y_idx < len(y_labels) and 0 <= x_idx < len(x_labels):
            matrix[y_labels[y_idx]][x_labels[x_idx]] = value
    for r_idx, y_label in enumerate(y_labels, start=1):
        ws.cell(row=header_row + r_idx, column=1, value=y_label).font = BODY_FONT
        for c_idx, x_label in enumerate(x_labels, start=2):
            value = matrix[y_label].get(x_label)
            ws.cell(row=header_row + r_idx, column=c_idx, value=value).font = BODY_FONT
    end_row = header_row + len(y_labels)
    end_col = 1 + len(x_labels)
    ws.conditional_formatting.add(
        f"B{header_row + 1}:{get_column_letter(end_col)}{end_row}",
        ColorScaleRule(start_type="num", start_value=-1, start_color="2E7D6F",
                       mid_type="num", mid_value=0, mid_color="F2F2F2",
                       end_type="num", end_value=1, end_color="C0392B"))
    return end_row + 2


def _source_text(block: dict) -> str:
    source = block.get("source") or {}
    if source.get("type") == "analysis":
        return (
            f"来源：{source.get('dataset_name')} v{source.get('dataset_version')}"
            f" · sha256 {str(source.get('dataset_sha256'))[:16]}…"
            f" · 模块 {source.get('module')}"
            f" · result_hash {source.get('result_hash')}"
            f" · chart_spec {source.get('chart_spec_hash') or '—'}")
    if source.get("type") == "sql":
        return (f"来源：SQL 只读查询 · sql_hash {source.get('sql_hash')}"
                f" · result_hash {source.get('result_hash')}")
    return "来源：叙述文本（非统计证据）"


def render_xlsx(spec: dict, report_dir: Path) -> Path:
    wb = Workbook()
    overview = wb.active
    overview.title = "报告概览"
    overview["A1"] = spec["title"]
    overview["A1"].font = Font(bold=True, size=16)
    overview["A2"] = spec.get("subtitle") or ""
    overview["A2"].font = Font(size=11, color="5F6B76")
    overview["A4"] = f"报告 ID：{spec['report_id']}"
    overview["A5"] = f"生成时间：{spec['created_at']}"
    overview["A6"] = f"spec_hash：{spec['spec_hash']}"
    for cell in ("A4", "A5", "A6"):
        overview[cell].font = MUTED_FONT
    row = _write_table(
        overview, 8,
        ["数据集", "dataset_id", "版本", "sha256", "行数", "列数"],
        [[d["name"], d["dataset_id"], d["version"], d["sha256"],
          d.get("rows", 0), d.get("columns", 0)] for d in spec.get("datasets") or []],
        100, title="数据来源")
    row = _write_table(
        overview, row + 2,
        ["块", "类型", "数据集", "版本", "sha256", "result_hash", "chart/sql hash"],
        [[f"{s.get('block_id')} · {s.get('block_title')}", s.get("type"),
          s.get("dataset_name") or "—", s.get("dataset_version") or "—",
          str(s.get("dataset_sha256") or "—")[:20], s.get("result_hash") or "—",
          s.get("chart_spec_hash") or s.get("sql_hash") or "—"]
         for s in spec.get("sources") or []],
        100, title="证据与来源")
    overview.cell(row=row + 2, column=1, value=spec.get("disclaimer") or "").font = MUTED_FONT
    overview.column_dimensions["A"].width = 26

    used: set[str] = {"报告概览"}
    limit = int(spec.get("max_table_rows") or 200)
    for index, block in enumerate(spec.get("blocks") or [], start=1):
        title = block.get("title") or f"块 {index}"
        ws = wb.create_sheet(_sheet_title(title, used, f"块{index}"))
        ws["A1"] = title
        ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = f"{block.get('type')} · {block.get('id')}"
        ws["A2"].font = MUTED_FONT
        row = 4
        if block["type"] == "text":
            ws["A4"] = block.get("text") or ""
            ws["A4"].alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells("A4:F4")
            ws.row_dimensions[4].height = 80
            row = 6
        elif block["type"] == "sql":
            row = _write_table(ws, row, block.get("columns") or [],
                               block.get("rows") or [], limit)
        else:
            table = (block.get("data") or {}).get("table") or {}
            row = _write_table(ws, row, table.get("columns") or [],
                               table.get("rows") or [], limit)
            chart = block.get("chart") or {}
            if chart.get("chart_type") == "heatmap":
                row = _write_heatmap(ws, block, row + 2)
            elif chart:
                _add_chart(ws, block, row + 2)
                row += 4 + len((chart.get("option") or {}).get("xAxis", {})
                               .get("data") or [])
        ws.cell(row=row + 1, column=1, value=_source_text(block)).font = MUTED_FONT
        for warning in block.get("warnings") or []:
            ws.cell(row=row + 2, column=1, value=f"提示：{warning}").font = MUTED_FONT

    output = report_dir / "report.xlsx"
    wb.save(str(output))
    return output
