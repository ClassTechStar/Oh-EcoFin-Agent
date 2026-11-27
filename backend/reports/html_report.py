"""HTML 报告（M5）：Jinja2 模板 + 内嵌 base64 图表，无外部 CDN。"""

from __future__ import annotations

import base64
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
# 模板名是 .html.j2，select_autoescape 不会命中，必须显式开启转义
_ENV = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


def render_html(spec: dict, report_dir: Path) -> Path:
    chart_images: dict[str, str] = {}
    for block in spec.get("blocks") or []:
        relative = block.get("chart_image") or ""
        if not relative:
            continue
        path = report_dir / relative
        if path.is_file():
            chart_images[block["id"]] = (
                "data:image/png;base64,"
                + base64.b64encode(path.read_bytes()).decode("ascii"))
    html = _ENV.get_template("report.html.j2").render(
        spec=spec, chart_images=chart_images)
    output = report_dir / "report.html"
    output.write_text(html, encoding="utf-8")
    return output
