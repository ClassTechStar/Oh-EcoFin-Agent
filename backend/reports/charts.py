"""Chart spec → PNG（M5）。

报告导出需要不依赖浏览器/外部 CDN 的图片，这里把 M4 的 ECharts option
用 matplotlib 离线绘制为 PNG；HTML 内嵌 base64，Word 直接插图，Excel 用
原生图表（另见 xlsx_report.py）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Polygon

PALETTE = ["#4f7cac", "#d08770", "#7fb069", "#b48ead", "#e0a458",
           "#5aa9a3", "#c45a5a", "#8ca6c0"]

matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC",
    "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False

_LOCK = threading.Lock()


def _num(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _series(option: dict) -> list[dict]:
    return [s for s in (option.get("series") or []) if isinstance(s, dict)]


def _categories(option: dict) -> list[str]:
    axis = option.get("xAxis") or {}
    data = axis.get("data") or []
    return [str(x) for x in data]


def _apply_axes(ax, title: str) -> None:
    ax.set_title(title, fontsize=13, pad=12)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _empty_figure(title: str):
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.axis("off")
    ax.text(0.5, 0.5, "暂无可绘制的数据", ha="center", va="center",
            fontsize=13, color="#8a949e")
    ax.set_title(title, fontsize=13)
    return fig


def _bar(option: dict, title: str):
    categories = _categories(option)
    series = _series(option)
    if not categories or not series:
        return _empty_figure(title)
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    x = np.arange(len(categories))
    width = 0.8 / max(len(series), 1)
    for idx, item in enumerate(series):
        values = [_num(v) for v in (item.get("data") or [])]
        offset = (idx - (len(series) - 1) / 2) * width
        bars = ax.bar(x + offset, values, width=width,
                      color=PALETTE[idx % len(PALETTE)],
                      label=str(item.get("name") or ""))
        if len(series) == 1:
            ax.bar_label(bars, fmt="%.4g", fontsize=8, padding=2)
    ax.set_xticks(x)
    ax.set_xticklabels(categories, rotation=30 if len(categories) > 8 else 0,
                       ha="right" if len(categories) > 8 else "center")
    y_name = (option.get("yAxis") or {}).get("name", "")
    if y_name:
        ax.set_ylabel(str(y_name), fontsize=9)
    if len(series) > 1:
        ax.legend(fontsize=8, frameon=False)
    _apply_axes(ax, title)
    fig.tight_layout()
    return fig


def _line(option: dict, title: str):
    categories = _categories(option)
    series = _series(option)
    if not categories or not series:
        return _empty_figure(title)
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    x = np.arange(len(categories))
    for idx, item in enumerate(series):
        values = np.array([
            np.nan if v is None or v == "" else _num(v)
            for v in (item.get("data") or [])
        ], dtype=float)
        ax.plot(x, values, linewidth=1.8, marker="o" if len(categories) <= 30 else None,
                markersize=3, color=PALETTE[idx % len(PALETTE)],
                label=str(item.get("name") or ""))
    ax.set_xticks(x)
    ax.set_xticklabels(categories, rotation=30 if len(categories) > 10 else 0,
                       ha="right" if len(categories) > 10 else "center")
    y_name = (option.get("yAxis") or {}).get("name", "")
    if y_name:
        ax.set_ylabel(str(y_name), fontsize=9)
    if len(series) > 1:
        ax.legend(fontsize=8, frameon=False)
    _apply_axes(ax, title)
    fig.tight_layout()
    return fig


def _pie(option: dict, title: str):
    series = _series(option)
    data = (series[0].get("data") if series else None) or []
    items = [(str(d.get("name", "")), _num(d.get("value")))
             for d in data if _num(d.get("value")) > 0]
    if not items:
        return _empty_figure(title)
    labels, values = zip(*items, strict=False)
    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(items))]
    if len(items) <= 5:
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90,
               colors=colors, textprops={"fontsize": 9})
    else:
        ax.pie(values, autopct="%1.1f%%", startangle=90, colors=colors,
               textprops={"fontsize": 9})
        ax.legend(labels, loc="center left", bbox_to_anchor=(1.0, 0.5),
                  fontsize=8, frameon=False)
    ax.set_title(title, fontsize=13)
    ax.axis("equal")
    fig.tight_layout()
    return fig


def _heatmap(option: dict, title: str):
    x_labels = [str(x) for x in (option.get("xAxis") or {}).get("data") or []]
    y_labels = [str(x) for x in (option.get("yAxis") or {}).get("data") or []]
    series = _series(option)
    pairs = (series[0].get("data") if series else None) or []
    if not x_labels or not y_labels or not pairs:
        return _empty_figure(title)
    matrix = np.full((len(y_labels), len(x_labels)), np.nan)
    for pair in pairs:
        try:
            x_idx, y_idx, value = int(pair[0]), int(pair[1]), float(pair[2])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= y_idx < matrix.shape[0] and 0 <= x_idx < matrix.shape[1]:
            matrix[y_idx, x_idx] = value
    cmap = LinearSegmentedColormap.from_list(
        "corr", ["#2e7d6f", "#f2f2f2", "#c0392b"])
    fig, ax = plt.subplots(figsize=(8.4, 6.2))
    image = ax.imshow(matrix, cmap=cmap, vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(x_labels)))
    ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(y_labels)))
    ax.set_yticklabels(y_labels, fontsize=8)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            if not np.isnan(matrix[i, j]):
                ax.text(j, i, f"{matrix[i, j]:.2f}", ha="center", va="center",
                        fontsize=7, color="#24303b")
    fig.colorbar(image, ax=ax, shrink=0.82)
    ax.set_title(title, fontsize=13, pad=12)
    fig.tight_layout()
    return fig


def _funnel(option: dict, title: str):
    series = _series(option)
    data = (series[0].get("data") if series else None) or []
    items = [(str(d.get("name", "")), _num(d.get("value"))) for d in data]
    items = [(name, value) for name, value in items if value > 0]
    if not items:
        return _empty_figure(title)
    first = max(value for _, value in items)
    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    count = len(items)
    for idx, (name, value) in enumerate(items):
        top = value / first
        if idx + 1 < count:
            bottom = max(items[idx + 1][1] / first, 0.05)
        else:
            bottom = max(top * 0.55, 0.05)
        y_top = count - idx
        y_bottom = y_top - 0.92
        polygon = Polygon([
            [-top / 2, y_top], [top / 2, y_top],
            [bottom / 2, y_bottom], [-bottom / 2, y_bottom],
        ], closed=True, color=PALETTE[idx % len(PALETTE)])
        ax.add_patch(polygon)
        ax.text(0, (y_top + y_bottom) / 2, f"{name}  {value:g}",
                ha="center", va="center", fontsize=9, color="white",
                fontweight="bold")
    ax.set_xlim(-0.65, 0.65)
    ax.set_ylim(0, count + 0.15)
    ax.axis("off")
    ax.set_title(title, fontsize=13, pad=10)
    fig.tight_layout()
    return fig


_BUILDERS = {
    "bar": _bar,
    "line": _line,
    "pie": _pie,
    "heatmap": _heatmap,
    "funnel": _funnel,
}


def render_chart_png(chart: dict, output: str | Path, dpi: int = 150) -> Path:
    """把 chart spec 渲染为 PNG；同一进程内串行调用 matplotlib。"""
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    option = chart.get("option") or {}
    title = chart.get("title") or (option.get("title") or {}).get("text", "")
    chart_type = chart.get("chart_type") or "bar"
    with _LOCK:
        fig = _BUILDERS.get(chart_type, _bar)(option, str(title))
        fig.savefig(out, dpi=dpi, bbox_inches="tight", facecolor="white",
                    metadata={"Software": "Oh-EcoFin-Agent"})
        plt.close(fig)
    return out
