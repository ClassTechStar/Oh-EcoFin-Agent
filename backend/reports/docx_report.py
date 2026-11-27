"""Word 报告（M5）：python-docx 生成，图表以 PNG 嵌入。"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

FONT = "Microsoft YaHei"


def _east_asia(run) -> None:
    run.font.name = FONT
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), FONT)


def _style_font(style, size: float | None = None) -> None:
    style.font.name = FONT
    if size is not None:
        style.font.size = Pt(size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), FONT)


def _shade(cell, color: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(shd)


def _cell_text(cell, value, *, bold: bool = False, size: float = 8.5) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    run = paragraph.add_run("—" if value is None else str(value))
    run.bold = bold
    run.font.size = Pt(size)
    _east_asia(run)


def _add_table(doc, columns: list[str], rows: list[list], limit: int) -> None:
    table = doc.add_table(rows=1, cols=len(columns))
    table.style = "Table Grid"
    table.autofit = True
    for idx, name in enumerate(columns):
        cell = table.rows[0].cells[idx]
        _cell_text(cell, name, bold=True)
        _shade(cell, "F4F6F8")
    for row in rows[:limit]:
        cells = table.add_row().cells
        for idx, value in enumerate(row):
            _cell_text(cells[idx], value)
    if len(rows) > limit:
        paragraph = doc.add_paragraph()
        run = paragraph.add_run(
            f"仅展示前 {limit} 行，共 {len(rows)} 行；完整数据见 Excel 导出。")
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x7A, 0x85, 0x90)
        _east_asia(run)


def _source_line(doc, block: dict) -> None:
    source = block.get("source") or {}
    if source.get("type") == "analysis":
        text = (
            f"数据来源：{source.get('dataset_name')} v{source.get('dataset_version')}"
            f" · sha256 {str(source.get('dataset_sha256'))[:16]}…"
            f" · 模块 {source.get('module')}"
            f" · result_hash {source.get('result_hash')}"
        )
        if source.get("chart_spec_hash"):
            text += f" · chart_spec {source.get('chart_spec_hash')}"
    elif source.get("type") == "sql":
        text = (f"SQL 只读查询 · sql_hash {source.get('sql_hash')}"
                f" · result_hash {source.get('result_hash')}")
    else:
        text = "叙述文本（非统计证据，数字审计见 M7）"
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.font.size = Pt(7.5)
    run.font.color.rgb = RGBColor(0x7A, 0x85, 0x90)
    _east_asia(run)


def render_docx(spec: dict, report_dir: Path) -> Path:
    doc = Document()
    for style_name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2"):
        try:
            _style_font(doc.styles[style_name])
        except KeyError:
            continue

    title = doc.add_heading(spec["title"], level=0)
    for run in title.runs:
        _east_asia(run)
    if spec.get("subtitle"):
        subtitle = doc.add_paragraph(spec["subtitle"])
        subtitle.runs[0].italic = True
        subtitle.runs[0].font.size = Pt(11)
        _east_asia(subtitle.runs[0])
    meta = doc.add_paragraph()
    run = meta.add_run(
        f"报告 ID：{spec['report_id']}　生成时间：{spec['created_at']}"
        f"　spec_hash：{spec['spec_hash']}")
    run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(0x7A, 0x85, 0x90)
    _east_asia(run)
    if spec.get("datasets"):
        chips = "　".join(
            f"{d['name']} v{d['version']} · {str(d['sha256'])[:12]}…"
            for d in spec["datasets"])
        paragraph = doc.add_paragraph()
        run = paragraph.add_run(chips)
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x44, 0x51, 0x5D)
        _east_asia(run)

    limit = int(spec.get("max_table_rows") or 200)
    for block in spec.get("blocks") or []:
        heading = doc.add_heading(block.get("title") or "分析", level=1)
        for run in heading.runs:
            _east_asia(run)
        if block["type"] == "text":
            for paragraph_text in str(block.get("text") or "").split("\n\n"):
                paragraph = doc.add_paragraph(paragraph_text)
                for run in paragraph.runs:
                    _east_asia(run)
        elif block["type"] == "sql":
            _add_table(doc, block.get("columns") or [], block.get("rows") or [], limit)
        else:
            table = (block.get("data") or {}).get("table") or {}
            _add_table(doc, table.get("columns") or [], table.get("rows") or [], limit)

        relative = block.get("chart_image") or ""
        if relative:
            image_path = report_dir / relative
            if image_path.is_file():
                doc.add_picture(str(image_path), width=Inches(6.2))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

        for warning in block.get("warnings") or []:
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(f"提示：{warning}")
            run.font.size = Pt(8.5)
            run.font.color.rgb = RGBColor(0x9A, 0x5B, 0x00)
            _east_asia(run)
        _source_line(doc, block)

    appendix = doc.add_heading("证据与来源", level=1)
    for run in appendix.runs:
        _east_asia(run)
    _add_table(
        doc,
        ["块", "类型", "数据集", "版本", "sha256", "result_hash", "chart/sql hash"],
        [[f"{s.get('block_id')} · {s.get('block_title')}", s.get("type"),
          s.get("dataset_name") or "—", s.get("dataset_version") or "—",
          str(s.get("dataset_sha256") or "—")[:20], s.get("result_hash") or "—",
          s.get("chart_spec_hash") or s.get("sql_hash") or "—"]
         for s in spec.get("sources") or []],
        100,
    )
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(spec.get("disclaimer") or "")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x5F, 0x6B, 0x76)
    _east_asia(run)

    output = report_dir / "report.docx"
    doc.save(str(output))
    return output
