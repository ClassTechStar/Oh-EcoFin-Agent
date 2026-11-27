"""M4 内置分析模块：描述/缺失/相关/趋势/分组/漏斗/A-B。

全部模块只读、确定性输出；图表为纯 JSON 的 ECharts option（不含 JS 函数），
前端直接 setOption，因此同一 dataset + params 的 spec_hash 可复现。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats as scipy_stats

from backend.analysis.common import (
    ensure_columns,
    fmt_value,
    ident,
    is_numeric,
    numeric_columns,
    table_spec,
)
from backend.analysis.models import AnalysisError

PALETTE = ["#4f7cac", "#d08770", "#7fb069", "#b48ead", "#e0a458",
           "#5aa9a3", "#c45a5a", "#8ca6c0"]


@dataclass
class AnalysisContext:
    conn: duckdb.DuckDBPyConnection
    relation: str
    schema: dict[str, str]
    params: Any
    max_describe_columns: int = 50
    max_correlation_columns: int = 12
    max_trend_points: int = 500
    max_groups: int = 10
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    label: str
    description: str
    params_model: type[BaseModel]
    params_meta: list[dict]
    run: Callable[[AnalysisContext], dict]


def _meta(name: str, label: str, kind: str, *, required: bool = False,
          default: Any = None, options: list[str] | None = None,
          column_types: list[str] | None = None, help: str = "",
          split: bool = False) -> dict:
    item: dict[str, Any] = {
        "name": name, "label": label, "kind": kind, "required": required,
        "help": help,
    }
    if default is not None:
        item["default"] = default
    if options is not None:
        item["options"] = options
    if column_types is not None:
        item["column_types"] = column_types
    if split:
        item["split"] = True
    return item


def _axis_option(title: str, *, x_name: str = "", y_name: str = "") -> dict:
    return {
        "title": {"text": title, "left": "center",
                  "textStyle": {"fontSize": 14, "fontWeight": "normal"}},
        "color": PALETTE,
        "animation": False,
        "grid": {"left": 64, "right": 28, "top": 60, "bottom": 72},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": [], "name": x_name,
                  "axisLabel": {"rotate": 30, "interval": "auto"}},
        "yAxis": {"type": "value", "name": y_name},
        "series": [],
    }


def _total_rows(ctx: AnalysisContext) -> int:
    return int(ctx.conn.execute(
        f"SELECT COUNT(*) FROM {ctx.relation}").fetchone()[0])


def _require_rows(ctx: AnalysisContext) -> int:
    total = _total_rows(ctx)
    if total == 0:
        raise AnalysisError("数据集为空，无法分析", code="empty_dataset",
                            hint="先导入包含数据行的数据集")
    return total


def _no_result(message: str = "筛选后没有可分析的数据") -> AnalysisError:
    return AnalysisError(message, code="empty_result",
                         hint="检查过滤条件/列取值后重试")


# ---------------- 描述统计 ----------------

class DescribeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list, max_length=200)
    top_n: int = Field(5, ge=1, le=20)


DESCRIBE_TABLE = ["字段", "类型", "非空", "缺失", "唯一值", "均值", "标准差",
                  "最小", "P25", "中位数", "P75", "最大"]


def run_describe(ctx: AnalysisContext) -> dict:
    params: DescribeParams = ctx.params
    total = _require_rows(ctx)
    cols = params.columns or list(ctx.schema)[: ctx.max_describe_columns]
    if not cols:
        raise AnalysisError("没有可分析的列", code="no_columns")
    ensure_columns(ctx.schema, cols)
    numeric = [c for c in cols if is_numeric(ctx.schema[c])]
    categorical = [c for c in cols if c not in numeric]
    entries: dict[str, dict] = {}

    if numeric:
        exprs: list[str] = []
        for name in numeric:
            col = f"CAST({ident(name)} AS DOUBLE)"
            exprs.extend([
                f"COUNT({ident(name)})",
                f"COUNT(*) - COUNT({ident(name)})",
                f"COUNT(DISTINCT {ident(name)})",
                f"AVG({col})", f"STDDEV_SAMP({col})",
                f"MIN({col})",
                f"quantile_cont({col}, 0.25)", f"quantile_cont({col}, 0.50)",
                f"quantile_cont({col}, 0.75)", f"MAX({col})",
            ])
        values = ctx.conn.execute(
            "SELECT " + ", ".join(exprs) + f" FROM {ctx.relation}").fetchone()
        offset = 0
        for name in numeric:
            count, nulls, distinct, mean, std, min_v, p25, p50, p75, max_v = \
                values[offset:offset + 10]
            offset += 10
            entries[name] = {
                "name": name, "dtype": ctx.schema[name], "kind": "numeric",
                "count": int(count), "null_count": int(nulls),
                "null_ratio": round(int(nulls) / total, 6) if total else 0.0,
                "distinct_count": int(distinct),
                "mean": fmt_value(mean), "std": fmt_value(std),
                "min": fmt_value(min_v), "p25": fmt_value(p25),
                "p50": fmt_value(p50), "p75": fmt_value(p75),
                "max": fmt_value(max_v),
            }

    if categorical:
        exprs = []
        for name in categorical:
            exprs.extend([
                f"COUNT({ident(name)})",
                f"COUNT(*) - COUNT({ident(name)})",
                f"COUNT(DISTINCT {ident(name)})",
            ])
        values = ctx.conn.execute(
            "SELECT " + ", ".join(exprs) + f" FROM {ctx.relation}").fetchone()
        offset = 0
        for name in categorical:
            count, nulls, distinct = values[offset:offset + 3]
            offset += 3
            entry = {
                "name": name, "dtype": ctx.schema[name], "kind": "categorical",
                "count": int(count), "null_count": int(nulls),
                "null_ratio": round(int(nulls) / total, 6) if total else 0.0,
                "distinct_count": int(distinct),
                "mean": None, "std": None, "min": None, "p25": None,
                "p50": None, "p75": None, "max": None,
            }
            if len([c for c in categorical]) <= 20:
                top = ctx.conn.execute(
                    f"SELECT CAST({ident(name)} AS VARCHAR) AS v, COUNT(*) AS n"
                    f" FROM {ctx.relation} WHERE {ident(name)} IS NOT NULL"
                    " GROUP BY 1 ORDER BY n DESC, v ASC LIMIT ?",
                    [params.top_n]).fetchall()
                entry["top_values"] = [{"value": r[0], "count": int(r[1])} for r in top]
            entries[name] = entry

    rows = []
    for name in cols:
        e = entries[name]
        rows.append([e["name"], e["dtype"], e["count"], e["null_count"],
                     e["distinct_count"], e["mean"], e["std"], e["min"],
                     e["p25"], e["p50"], e["p75"], e["max"]])
    return {
        "data": {
            "rows": total,
            "columns": [entries[c] for c in cols],
            "numeric_count": len(numeric),
            "categorical_count": len(categorical),
            "table": table_spec(DESCRIBE_TABLE, rows),
        },
        "chart": None,
        "warnings": ctx.warnings,
    }


# ---------------- 缺失分析 ----------------

class MissingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list, max_length=512)


def run_missing(ctx: AnalysisContext) -> dict:
    params: MissingParams = ctx.params
    total = _require_rows(ctx)
    cols = params.columns or list(ctx.schema)
    ensure_columns(ctx.schema, cols)
    exprs = [f"COUNT(*) - COUNT({ident(c)})" for c in cols]
    nulls = ctx.conn.execute(
        "SELECT " + ", ".join(exprs) + f" FROM {ctx.relation}").fetchone()
    entries = [{
        "name": c,
        "dtype": ctx.schema[c],
        "null_count": int(n),
        "null_ratio": round(int(n) / total, 6) if total else 0.0,
    } for c, n in zip(cols, nulls, strict=False)]
    entries.sort(key=lambda e: (-e["null_count"], e["name"]))
    with_missing = [e for e in entries if e["null_count"] > 0]
    table = table_spec(
        ["字段", "类型", "缺失数", "缺失率"],
        [[e["name"], e["dtype"], e["null_count"],
          f"{e['null_ratio'] * 100:.2f}%"] for e in entries])
    chart = None
    if with_missing:
        top = with_missing[:20]
        opt = _axis_option("缺失率 Top 20（%）", y_name="缺失率 %")
        opt["xAxis"]["data"] = [e["name"] for e in top]
        opt["yAxis"]["min"] = 0
        opt["yAxis"]["max"] = 100
        opt["series"] = [{
            "type": "bar", "name": "缺失率",
            "data": [round(e["null_ratio"] * 100, 4) for e in top],
            "barMaxWidth": 40,
            "label": {"show": True, "position": "top", "formatter": "{c}%"},
        }]
        chart = {"chart_type": "bar", "title": "缺失率 Top 20（%）", "option": opt}
    return {
        "data": {
            "rows": total,
            "columns": entries,
            "columns_with_missing": len(with_missing),
            "total_missing": sum(e["null_count"] for e in entries),
            "table": table,
        },
        "chart": chart,
        "warnings": ctx.warnings,
    }


# ---------------- 相关性 ----------------

class CorrelationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list, max_length=30)
    method: Literal["pearson"] = "pearson"


def run_correlation(ctx: AnalysisContext) -> dict:
    params: CorrelationParams = ctx.params
    total = _require_rows(ctx)
    cols = params.columns or numeric_columns(ctx.schema)
    ensure_columns(ctx.schema, cols, kind="numeric")
    if len(cols) > ctx.max_correlation_columns:
        ctx.warnings.append(
            f"数值列超过 {ctx.max_correlation_columns} 个，已截断")
        cols = cols[: ctx.max_correlation_columns]
    if len(cols) < 2:
        raise AnalysisError("至少需要 2 个数值列计算相关性", code="too_few_columns",
                            hint="在参数中显式选择 2 个以上数值列")
    pairs = [(i, j) for i in range(len(cols)) for j in range(i + 1, len(cols))]
    exprs = [
        f"corr(CAST({ident(cols[i])} AS DOUBLE), CAST({ident(cols[j])} AS DOUBLE))"
        for i, j in pairs
    ]
    values = ctx.conn.execute(
        "SELECT " + ", ".join(exprs) + f" FROM {ctx.relation}").fetchone()
    matrix = [[None] * len(cols) for _ in cols]
    for idx, (i, j) in enumerate(pairs):
        value = values[idx]
        if value is not None and not math.isnan(value):
            matrix[i][j] = matrix[j][i] = round(float(value), 6)
    for i in range(len(cols)):
        matrix[i][i] = 1.0
    heat = []
    for i in range(len(cols)):
        for j in range(len(cols)):
            if matrix[i][j] is not None:
                heat.append([j, i, matrix[i][j]])
    option = {
        "title": {"text": "相关系数矩阵（Pearson）", "left": "center",
                  "textStyle": {"fontSize": 14, "fontWeight": "normal"}},
        "animation": False,
        "tooltip": {"position": "top"},
        "grid": {"left": 96, "right": 28, "top": 60, "bottom": 88},
        "xAxis": {"type": "category", "data": cols, "axisLabel": {"rotate": 45}},
        "yAxis": {"type": "category", "data": cols},
        "visualMap": {
            "min": -1, "max": 1, "calculable": True, "orient": "horizontal",
            "left": "center", "bottom": 8,
            "inRange": {"color": ["#2e7d6f", "#f2f2f2", "#c0392b"]},
        },
        "series": [{
            "type": "heatmap", "data": heat,
            "label": {"show": True, "formatter": "{c}"},
        }],
    }
    table = table_spec(["字段"] + cols,
                       [[cols[i]] + matrix[i] for i in range(len(cols))])
    return {
        "data": {
            "columns": cols, "matrix": matrix, "method": "pearson",
            "rows": total, "table": table,
        },
        "chart": {"chart_type": "heatmap", "title": "相关系数矩阵（Pearson）",
                  "option": option},
        "warnings": ctx.warnings,
    }


# ---------------- 趋势 ----------------

class TrendParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_column: str = Field(..., min_length=1, max_length=128)
    value_column: str = Field("", max_length=128)
    agg: Literal["sum", "avg", "count", "min", "max"] = "sum"
    bucket: Literal["day", "week", "month", "quarter", "year"] = "month"
    group_by: str = Field("", max_length=128)
    limit: int = Field(200, ge=2, le=1000)


_TREND_AGG = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}


def _bucket_expr(bucket: str, date_expr: str) -> str:
    if bucket == "day":
        return f"strftime({date_expr}, '%Y-%m-%d')"
    if bucket == "week":
        return f"strftime(date_trunc('week', {date_expr}), '%Y-%m-%d')"
    if bucket == "month":
        return f"strftime({date_expr}, '%Y-%m')"
    if bucket == "quarter":
        return (f"strftime({date_expr}, '%Y') || '-Q' || "
                f"CAST(quarter({date_expr}) AS VARCHAR)")
    return f"strftime({date_expr}, '%Y')"


def run_trend(ctx: AnalysisContext) -> dict:
    params: TrendParams = ctx.params
    _require_rows(ctx)
    ensure_columns(ctx.schema, [params.date_column], kind="date")
    if params.agg != "count":
        if not params.value_column:
            raise AnalysisError(
                f"聚合方式 {params.agg} 需要 value_column", code="missing_param",
                hint="选择数值列，或把 agg 改为 count")
        ensure_columns(ctx.schema, [params.value_column], kind="numeric")
    if params.group_by:
        ensure_columns(ctx.schema, [params.group_by])

    date_col = ident(params.date_column)
    date_expr = f"TRY_CAST({date_col} AS DATE)"
    where = f"{date_expr} IS NOT NULL"
    if params.group_by:
        where += f" AND {ident(params.group_by)} IS NOT NULL"
    if params.agg != "count" and params.value_column:
        where += f" AND {ident(params.value_column)} IS NOT NULL"
    value_expr = "COUNT(*)" if params.agg == "count" else (
        f"{_TREND_AGG[params.agg]}(CAST({ident(params.value_column)} AS DOUBLE))")
    group_select = (f"CAST({ident(params.group_by)} AS VARCHAR) AS grp,"
                    if params.group_by else "")
    group_by = ", 2" if params.group_by else ""
    group_order = ", agg.grp" if params.group_by else ""
    sql = f"""
        WITH d AS (
          SELECT {date_expr} AS d
                 {',' + ident(params.group_by) if params.group_by else ''}
                 {',' + ident(params.value_column) if params.agg != 'count' else ''},
                 {_bucket_expr(params.bucket, date_expr)} AS bucket
          FROM {ctx.relation}
          WHERE {where}
        ), agg AS (
          SELECT bucket, {group_select} {value_expr} AS value, COUNT(*) AS n
          FROM d GROUP BY 1{group_by}
        ), recent AS (
          SELECT DISTINCT bucket FROM agg ORDER BY bucket DESC LIMIT ?
        )
        SELECT agg.bucket, {('agg.grp,' if params.group_by else '')}
               agg.value, agg.n
        FROM agg JOIN recent USING(bucket)
        ORDER BY agg.bucket{group_order}
    """
    raw_rows = ctx.conn.execute(
        sql, [min(params.limit, ctx.max_trend_points)]).fetchall()
    if not raw_rows:
        raise _no_result()
    if params.group_by:
        rows = [(r[0], r[1], r[2], r[3]) for r in raw_rows]
    else:
        rows = [(r[0], "", r[1], r[2]) for r in raw_rows]
    buckets = sorted({r[0] for r in rows})
    totals: dict[str, float] = {}
    for _bucket, grp, value, _n in rows:
        key = grp or ""
        totals[key] = totals.get(key, 0.0) + abs(float(value or 0))
    ordered = sorted(totals, key=lambda g: (-totals[g], g))
    if len(ordered) > ctx.max_groups:
        ctx.warnings.append(
            f"分组数 {len(ordered)} 超过 {ctx.max_groups}，仅展示总量最高的 "
            f"{ctx.max_groups} 组")
        ordered = ordered[: ctx.max_groups]
    lookup = {(r[0], (r[1] or "")): (fmt_value(r[2]), int(r[3])) for r in rows}
    display = params.value_column or "计数"
    series = []
    for grp in ordered:
        values = [lookup.get((b, grp), (None, 0))[0] for b in buckets]
        counts = [lookup.get((b, grp), (None, 0))[1] for b in buckets]
        series.append({"name": grp or display, "values": values, "counts": counts})
    table_rows = []
    for idx, bucket in enumerate(buckets):
        table_rows.append([bucket] + [s["values"][idx] for s in series])
    option = _axis_option(
        f"趋势 · {params.agg}（按{_bucket_label(params.bucket)}）",
        x_name="时间", y_name=display)
    option["legend"] = {"top": 28, "type": "scroll"}
    option["xAxis"]["data"] = buckets
    option["series"] = [{
        "type": "line", "name": s["name"], "data": s["values"],
        "connectNulls": False, "showSymbol": len(buckets) <= 60,
    } for s in series]
    return {
        "data": {
            "x": buckets, "series": series, "bucket": params.bucket,
            "agg": params.agg, "date_column": params.date_column,
            "value_column": params.value_column,
            "table": table_spec(["时间"] + [s["name"] for s in series], table_rows),
        },
        "chart": {"chart_type": "line",
                  "title": f"趋势 · {params.agg}（按{_bucket_label(params.bucket)}）",
                  "option": option},
        "warnings": ctx.warnings,
    }


def _bucket_label(bucket: str) -> str:
    return {"day": "日", "week": "周", "month": "月",
            "quarter": "季度", "year": "年"}[bucket]


# ---------------- 分组聚合 ----------------

class GroupParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_column: str = Field(..., min_length=1, max_length=128)
    value_column: str = Field("", max_length=128)
    agg: Literal["count", "sum", "avg", "min", "max", "median"] = "count"
    top_n: int = Field(20, ge=1, le=100)
    chart_type: Literal["bar", "pie"] = "bar"


_GROUP_AGG = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX",
              "median": "MEDIAN"}


def run_group(ctx: AnalysisContext) -> dict:
    params: GroupParams = ctx.params
    _require_rows(ctx)
    ensure_columns(ctx.schema, [params.group_column])
    if params.agg != "count":
        if not params.value_column:
            raise AnalysisError(
                f"聚合方式 {params.agg} 需要 value_column", code="missing_param")
        ensure_columns(ctx.schema, [params.value_column], kind="numeric")
    group = ident(params.group_column)
    where = f"{group} IS NOT NULL"
    value_expr = "COUNT(*)" if params.agg == "count" else (
        f"{_GROUP_AGG[params.agg]}(CAST({ident(params.value_column)} AS DOUBLE))")
    if params.agg != "count":
        where += f" AND {ident(params.value_column)} IS NOT NULL"
    rows = ctx.conn.execute(
        f"SELECT CAST({group} AS VARCHAR) AS label, {value_expr} AS value,"
        f" COUNT(*) AS n FROM {ctx.relation} WHERE {where}"
        " GROUP BY 1 ORDER BY value DESC, label ASC LIMIT ?",
        [params.top_n + 1]).fetchall()
    if not rows:
        raise _no_result()
    truncated = len(rows) > params.top_n
    rows = rows[: params.top_n]
    if truncated:
        ctx.warnings.append(f"仅展示前 {params.top_n} 个分组")

    total_row = ctx.conn.execute(
        f"SELECT COUNT(*) AS n, {value_expr if params.agg != 'count' else 'NULL'}"
        f" AS total FROM {ctx.relation} WHERE {where}").fetchone()
    total_n = int(total_row[0])
    total_value = total_row[1] if params.agg in ("sum", "count") else None
    items = []
    for label, value, n in rows:
        value = fmt_value(value)
        share = None
        if total_value not in (None, 0) and value is not None:
            share = round(float(value) / float(total_value), 6)
        elif params.agg == "count" and total_n:
            share = round(int(n) / total_n, 6)
        items.append({"label": label, "value": value, "count": int(n),
                      "share": share})
    chart_type = params.chart_type
    if chart_type == "pie" and any((i["value"] or 0) < 0 for i in items):
        ctx.warnings.append("存在负值，饼图已自动切换为柱状图")
        chart_type = "bar"
    title = f"{params.group_column} · {params.agg}"
    if chart_type == "pie":
        option = {
            "title": {"text": title, "left": "center",
                      "textStyle": {"fontSize": 14, "fontWeight": "normal"}},
            "color": PALETTE, "animation": False,
            "tooltip": {"trigger": "item"},
            "legend": {"type": "scroll", "bottom": 0},
            "series": [{
                "type": "pie", "radius": ["35%", "62%"], "center": ["50%", "52%"],
                "data": [{"name": str(i["label"]), "value": i["value"]}
                         for i in items],
                "label": {"formatter": "{b}: {d}%"},
            }],
        }
    else:
        option = _axis_option(title, y_name=params.value_column or "计数")
        option["xAxis"]["data"] = [str(i["label"]) for i in items]
        option["series"] = [{
            "type": "bar", "name": params.value_column or "计数",
            "data": [i["value"] for i in items], "barMaxWidth": 44,
        }]
    table = table_spec(
        ["分组", "值", "样本数", "占比"],
        [[i["label"], i["value"], i["count"],
          f"{i['share'] * 100:.2f}%" if i["share"] is not None else "—"]
         for i in items])
    return {
        "data": {
            "group_column": params.group_column,
            "value_column": params.value_column,
            "agg": params.agg,
            "items": items,
            "total_rows": total_n,
            "truncated": truncated,
            "table": table,
        },
        "chart": {"chart_type": chart_type, "title": title, "option": option},
        "warnings": ctx.warnings,
    }


# ---------------- 漏斗 ----------------

class FunnelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_column: str = Field(..., min_length=1, max_length=128)
    stages: list[str] = Field(default_factory=list, max_length=50)
    value_column: str = Field("", max_length=128)


def run_funnel(ctx: AnalysisContext) -> dict:
    params: FunnelParams = ctx.params
    _require_rows(ctx)
    ensure_columns(ctx.schema, [params.stage_column])
    if params.value_column:
        ensure_columns(ctx.schema, [params.value_column], kind="numeric")
        value_expr = f"SUM(CAST({ident(params.value_column)} AS DOUBLE))"
        basis = "sum"
    else:
        value_expr = "COUNT(*)"
        basis = "count"
    stage = ident(params.stage_column)
    rows = ctx.conn.execute(
        f"SELECT CAST({stage} AS VARCHAR) AS label, {value_expr} AS value"
        f" FROM {ctx.relation} WHERE {stage} IS NOT NULL"
        " GROUP BY 1").fetchall()
    if not rows:
        raise _no_result("阶段列没有有效取值")
    values = {str(r[0]): fmt_value(r[1]) for r in rows}
    if params.stages:
        ordered = [s for s in dict.fromkeys(params.stages) if s in values]
        missing = [s for s in params.stages if s not in values]
        if missing:
            ctx.warnings.append(f"数据中不存在阶段: {missing}")
        if not ordered:
            raise _no_result("指定阶段在数据中均不存在")
    else:
        ordered = sorted(values, key=lambda s: (-(values[s] or 0), s))
    items = [{"stage": s, "value": values[s]} for s in ordered]
    if any((i["value"] or 0) < 0 for i in items):
        raise AnalysisError("漏斗数值包含负数，无法计算转化率",
                            code="negative_value")
    first = items[0]["value"] or 0
    if first <= 0:
        raise AnalysisError("漏斗首环节为 0，无法计算转化率",
                            code="zero_funnel")
    prev = first
    for item in items:
        value = item["value"] or 0
        item["rate_from_first"] = round(value / first, 6)
        item["rate_from_prev"] = round(value / prev, 6) if prev else None
        prev = value
    title = f"漏斗 · {params.stage_column}" + (
        f"（{params.value_column}）" if params.value_column else "（计数）")
    option = {
        "title": {"text": title, "left": "center",
                  "textStyle": {"fontSize": 14, "fontWeight": "normal"}},
        "color": PALETTE, "animation": False,
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}"},
        "legend": {"type": "scroll", "bottom": 0},
        "series": [{
            "type": "funnel", "left": "10%", "right": "10%",
            "top": 60, "bottom": 32, "sort": "none", "gap": 2,
            "label": {"show": True, "position": "inside"},
            "data": [{"name": i["stage"], "value": i["value"]} for i in items],
        }],
    }
    table = table_spec(
        ["阶段", "值", "相对首环节", "相对上一环节"],
        [[i["stage"], i["value"],
          f"{i['rate_from_first'] * 100:.2f}%",
          f"{i['rate_from_prev'] * 100:.2f}%" if i["rate_from_prev"] is not None else "—"]
         for i in items])
    return {
        "data": {"items": items, "basis": basis,
                 "value_column": params.value_column, "table": table},
        "chart": {"chart_type": "funnel", "title": title, "option": option},
        "warnings": ctx.warnings,
    }


# ---------------- A/B 测试 ----------------

class ABTestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_column: str = Field(..., min_length=1, max_length=128)
    metric_column: str = Field(..., min_length=1, max_length=128)
    baseline: str = Field("", max_length=200)
    alpha: float = Field(0.05, gt=0, lt=1)
    alternative: Literal["two-sided", "greater", "less"] = "two-sided"


def _p_value(stat: float, *, alternative: str, df: float | None = None) -> float:
    dist = scipy_stats.t if df is not None else scipy_stats.norm
    if alternative == "two-sided":
        p = 2 * dist.sf(abs(stat), df) if df is not None else 2 * dist.sf(abs(stat))
    elif alternative == "greater":
        p = dist.sf(stat, df) if df is not None else dist.sf(stat)
    else:
        p = dist.cdf(stat, df) if df is not None else dist.cdf(stat)
    return float(min(max(p, 0.0), 1.0))


def run_ab_test(ctx: AnalysisContext) -> dict:
    params: ABTestParams = ctx.params
    _require_rows(ctx)
    ensure_columns(ctx.schema, [params.group_column], kind="any")
    ensure_columns(ctx.schema, [params.metric_column], kind="numeric")
    group = ident(params.group_column)
    metric = ident(params.metric_column)
    rows = ctx.conn.execute(
        f"SELECT CAST({group} AS VARCHAR) AS label, COUNT(*) AS n,"
        f" COUNT({metric}) AS nn, AVG(CAST({metric} AS DOUBLE)) AS mean,"
        f" STDDEV_SAMP(CAST({metric} AS DOUBLE)) AS std,"
        f" MIN(CAST({metric} AS DOUBLE)) AS min_v,"
        f" MAX(CAST({metric} AS DOUBLE)) AS max_v,"
        f" SUM(CASE WHEN CAST({metric} AS DOUBLE) NOT IN (0,1)"
        f"          OR CAST({metric} AS DOUBLE) IS NULL THEN 1 ELSE 0 END) AS non_binary,"
        f" SUM(CASE WHEN CAST({metric} AS DOUBLE)=1 THEN 1 ELSE 0 END) AS successes"
        f" FROM {ctx.relation}"
        f" WHERE {group} IS NOT NULL AND {metric} IS NOT NULL"
        " GROUP BY 1 ORDER BY 1").fetchall()
    if len(rows) != 2:
        raise AnalysisError(
            f"A/B 测试要求分组列恰好 2 个取值，当前 {len(rows)} 个",
            code="group_count", hint="选择只有两组的分组列，或先过滤数据")
    labels = [str(r[0]) for r in rows]
    if params.baseline:
        if params.baseline not in labels:
            raise AnalysisError(
                f"baseline 不在分组取值中: {params.baseline}", code="invalid_baseline",
                hint=f"可用取值: {', '.join(labels)}")
        baseline_label = params.baseline
    else:
        baseline_label = labels[0]
    treatment_label = labels[1] if baseline_label == labels[0] else labels[0]
    raw = {str(r[0]): r for r in rows}
    base, treat = raw[baseline_label], raw[treatment_label]

    nn1, mean1, std1 = int(base[2]), base[3], base[4]
    nn2, mean2, std2 = int(treat[2]), treat[3], treat[4]
    non_binary = int(base[7] or 0) + int(treat[7] or 0)
    min_v = min(float(base[5]), float(treat[5]))
    max_v = max(float(base[6]), float(treat[6]))
    metric_type = ("proportion" if non_binary == 0 and min_v >= 0 and max_v <= 1
                   else "mean")
    diff = float(mean2) - float(mean1)
    if metric_type == "mean":
        if nn1 < 2 or nn2 < 2:
            raise AnalysisError("均值 A/B 每组至少需要 2 个样本", code="sample_too_small")
        var1 = float(std1 or 0) ** 2
        var2 = float(std2 or 0) ** 2
        se = math.sqrt(var1 / nn1 + var2 / nn2)
        if se == 0:
            raise AnalysisError("两组数据均无方差，无法检验", code="zero_variance")
        stat = diff / se
        df = (var1 / nn1 + var2 / nn2) ** 2 / (
            (var1 / nn1) ** 2 / max(nn1 - 1, 1)
            + (var2 / nn2) ** 2 / max(nn2 - 1, 1))
        p_value = _p_value(stat, alternative=params.alternative, df=df)
        t_crit = float(scipy_stats.t.ppf(1 - params.alpha / 2, df))
        ci_low, ci_high = diff - t_crit * se, diff + t_crit * se
        pooled_std = math.sqrt(
            ((nn1 - 1) * var1 + (nn2 - 1) * var2) / max(nn1 + nn2 - 2, 1))
        effect = diff / pooled_std if pooled_std else None
        stat_name = "welch_t"
    else:
        p1 = float(mean1)
        p2 = float(mean2)
        s1 = int(base[8] or 0)
        s2 = int(treat[8] or 0)
        pooled = (s1 + s2) / (nn1 + nn2)
        se_pool = math.sqrt(pooled * (1 - pooled) * (1 / nn1 + 1 / nn2))
        if se_pool == 0:
            raise AnalysisError("两组转化率完全相同（0 或 100%），无法检验",
                                code="zero_variance")
        stat = diff / se_pool
        p_value = _p_value(stat, alternative=params.alternative)
        z_crit = float(scipy_stats.norm.ppf(1 - params.alpha / 2))
        se_diff = math.sqrt(p1 * (1 - p1) / nn1 + p2 * (1 - p2) / nn2)
        ci_low, ci_high = diff - z_crit * se_diff, diff + z_crit * se_diff
        effect = (2 * math.asin(math.sqrt(max(p2, 0)))
                  - 2 * math.asin(math.sqrt(max(p1, 0))))
        stat_name = "two_proportion_z"
    relative_lift = diff / float(mean1) if mean1 else None
    if nn1 < 30 or nn2 < 30:
        ctx.warnings.append("每组样本量小于 30，结论仅供参考")
    level = round(1 - params.alpha, 6)
    groups = [
        {"label": baseline_label, "role": "baseline", "n": nn1,
         "value": fmt_value(mean1), "std": fmt_value(std1),
         "successes": int(base[8] or 0)},
        {"label": treatment_label, "role": "treatment", "n": nn2,
         "value": fmt_value(mean2), "std": fmt_value(std2),
         "successes": int(treat[8] or 0)},
    ]
    value_name = "转化率" if metric_type == "proportion" else "均值"
    title = f"A/B 测试 · {params.metric_column}（{value_name}）"
    option = _axis_option(title, x_name="组别", y_name=value_name)
    option["xAxis"]["data"] = [baseline_label, treatment_label]
    option["series"] = [{
        "type": "bar", "name": value_name,
        "data": [fmt_value(mean1), fmt_value(mean2)], "barMaxWidth": 72,
        "label": {"show": True, "position": "top", "formatter": "{c}"},
    }]
    table = table_spec(
        ["组别", "样本数", value_name, "标准差", "成功数"],
        [[g["label"], g["n"], g["value"], g["std"], g["successes"]]
         for g in groups])
    return {
        "data": {
            "baseline": groups[0], "treatment": groups[1],
            "diff": fmt_value(diff), "relative_lift": fmt_value(relative_lift),
            "p_value": fmt_value(p_value), "significant": bool(p_value < params.alpha),
            "alpha": params.alpha, "alternative": params.alternative,
            "metric_type": metric_type, "test": stat_name,
            "effect_size": fmt_value(effect),
            "ci": {"low": fmt_value(ci_low), "high": fmt_value(ci_high),
                   "level": level},
            "table": table,
        },
        "chart": {"chart_type": "bar", "title": title, "option": option},
        "warnings": ctx.warnings,
    }


# ---------------- 注册表 ----------------

MODULES: dict[str, ModuleSpec] = {
    "describe": ModuleSpec(
        "describe", "描述统计",
        "数值列均值/标准差/分位，分类列唯一值与 Top 值。",
        DescribeParams,
        [
            _meta("columns", "列（留空=全部）", "columns",
                  column_types=["any"], help="可多选；不选则取前 50 列"),
            _meta("top_n", "分类 Top N", "number", default=5),
        ],
        run_describe,
    ),
    "missing": ModuleSpec(
        "missing", "缺失分析",
        "逐列缺失数与缺失率，附缺失率 Top 20 柱状图。",
        MissingParams,
        [_meta("columns", "列（留空=全部）", "columns", column_types=["any"])],
        run_missing,
    ),
    "correlation": ModuleSpec(
        "correlation", "相关性",
        "数值列 Pearson 相关矩阵（最多 12 列）。",
        CorrelationParams,
        [_meta("columns", "数值列（留空=全部数值列）", "columns",
               column_types=["numeric"])],
        run_correlation,
    ),
    "trend": ModuleSpec(
        "trend", "趋势分析",
        "按日/周/月/季/年聚合时间序列，可分组为多条折线。",
        TrendParams,
        [
            _meta("date_column", "日期列", "column", required=True,
                  column_types=["date"]),
            _meta("value_column", "数值列（count 可留空）", "column",
                  column_types=["numeric"]),
            _meta("agg", "聚合方式", "enum", default="sum",
                  options=["sum", "avg", "count", "min", "max"]),
            _meta("bucket", "时间粒度", "enum", default="month",
                  options=["day", "week", "month", "quarter", "year"]),
            _meta("group_by", "分组列（可选）", "column", column_types=["any"]),
            _meta("limit", "最大时间点", "number", default=200),
        ],
        run_trend,
    ),
    "group": ModuleSpec(
        "group", "分组聚合",
        "按分类列聚合计数/求和/均值等，支持柱状图与饼图。",
        GroupParams,
        [
            _meta("group_column", "分组列", "column", required=True,
                  column_types=["any"]),
            _meta("value_column", "数值列（count 可留空）", "column",
                  column_types=["numeric"]),
            _meta("agg", "聚合方式", "enum", default="count",
                  options=["count", "sum", "avg", "min", "max", "median"]),
            _meta("top_n", "Top N", "number", default=20),
            _meta("chart_type", "图表", "enum", default="bar",
                  options=["bar", "pie"]),
        ],
        run_group,
    ),
    "funnel": ModuleSpec(
        "funnel", "漏斗分析",
        "按阶段列计算环节值与相对首/上一环节转化率。",
        FunnelParams,
        [
            _meta("stage_column", "阶段列", "column", required=True,
                  column_types=["any"]),
            _meta("stages", "阶段顺序（逗号分隔，可选）", "string", split=True,
                  help="留空则按数值降序"),
            _meta("value_column", "数值列（留空=计数）", "column",
                  column_types=["numeric"]),
        ],
        run_funnel,
    ),
    "ab_test": ModuleSpec(
        "ab_test", "A/B 测试",
        "两组均值 Welch t 检验或两比例 z 检验，输出效应量、p 值与置信区间。",
        ABTestParams,
        [
            _meta("group_column", "分组列（恰好 2 组）", "column", required=True,
                  column_types=["any"]),
            _meta("metric_column", "指标列（数值/0-1）", "column", required=True,
                  column_types=["numeric"]),
            _meta("baseline", "对照组取值（可选）", "string"),
            _meta("alpha", "显著性水平", "number", default=0.05),
            _meta("alternative", "备择假设", "enum", default="two-sided",
                  options=["two-sided", "greater", "less"]),
        ],
        run_ab_test,
    ),
}


def module_infos() -> list[dict]:
    return [{
        "name": spec.name,
        "label": spec.label,
        "description": spec.description,
        "params": spec.params_meta,
    } for spec in MODULES.values()]
