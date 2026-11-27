"""结果压缩器（token 经济学）：K线/快照/新闻/财务/回测 → 统计量降维。

纯函数（好单测）；压缩前后样本对照固化在 tests/golden/。
原始完整结果照常进 tool_calls 表——溯源与幻觉率度量以原始返回为准。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ---------- K 线：500 根 OHLCV → ≤300 tokens ----------

def compact_kline(result: dict) -> dict:
    """K线 → 趋势方向/区间涨跌幅/年化波动/均线距离/支撑阻力/量能摘要。"""
    bars = result.get("bars") or result.get("data") or []
    if not bars:
        return {"ok": False, "summary": "K线数据为空"}
    df = pd.DataFrame(bars)
    for col in ("close", "high", "low", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    if df.empty:
        return {"ok": False, "summary": "K线数据无效"}

    close = df["close"]
    first, last = float(close.iloc[0]), float(close.iloc[-1])
    period_pct = (last / first - 1) * 100 if first else 0.0
    ret = close.pct_change().dropna()
    ann_vol = float(ret.std() * np.sqrt(244)) * 100 if len(ret) > 1 else 0.0

    trend = "up" if period_pct > 3 else ("down" if period_pct < -3 else "flat")

    def ma(n: int) -> float | None:
        return float(close.tail(n).mean()) if len(close) >= n else None

    ma20, ma60 = ma(20), ma(60)

    # 近 5 支撑/阻力位：局部极值（简化：滚动窗口高低点取尾部）
    recent = df.tail(120)
    supports = sorted({round(float(x), 2) for x in recent["low"].nlargest(0)} or set())
    lows = recent["low"].rolling(5, min_periods=1).min().dropna().unique()
    highs = recent["high"].rolling(5, min_periods=1).max().dropna().unique()
    supports = sorted({round(float(x), 2) for x in lows})[-5:]
    resistances = sorted({round(float(x), 2) for x in highs})[-5:]

    vol = df["volume"] if "volume" in df.columns else None
    vol_note = "n/a"
    if vol is not None and len(vol) >= 40:
        v5 = float(vol.tail(5).mean())
        v20 = float(vol.tail(20).mean())
        if v20:
            ratio = v5 / v20
            vol_note = "放量" if ratio > 1.2 else ("缩量" if ratio < 0.8 else "平量")

    return {
        "ok": True,
        "kind": "kline",
        "bars_count": len(df),
        "start": str(df.iloc[0].get("date", ""))[:10],
        "end": str(df.iloc[-1].get("date", ""))[:10],
        "last_close": round(last, 2),
        "period_pct": round(period_pct, 2),
        "trend": trend,
        "ann_vol_pct": round(ann_vol, 1),
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "ma20_dist_pct": round((last / ma20 - 1) * 100, 2) if ma20 else None,
        "ma60_dist_pct": round((last / ma60 - 1) * 100, 2) if ma60 else None,
        "supports": supports,
        "resistances": resistances,
        "volume_note": vol_note,
        "data_as_of": result.get("data_as_of", ""),
        "is_stale": result.get("is_stale", False),
    }


# ---------- 快照行：→ ≤120 tokens ----------

QUOTE_FIELDS = ["现价", "涨跌幅", "量比", "换手率", "总市值", "市盈率", "市净率"]


def compact_quote(result: dict) -> dict:
    """快照 → 现价/涨跌幅/量比/换手/市值/PE/PB 六项。"""
    row = result.get("quote") or result
    out: dict[str, Any] = {"ok": bool(row), "kind": "quote"}
    if not row:
        out["summary"] = "快照无此标的"
        return out
    mapping = {
        "price": row.get("price") or row.get("现价") or row.get("最新价"),
        "pct_chg": row.get("pct_chg") or row.get("涨跌幅"),
        "volume_ratio": row.get("volume_ratio") or row.get("量比"),
        "turnover": row.get("turnover") or row.get("换手率"),
        "market_cap": row.get("market_cap") or row.get("总市值"),
        "pe": row.get("pe") or row.get("市盈率") or row.get("市盈率-动态"),
        "pb": row.get("pb") or row.get("市净率"),
    }
    for k, v in mapping.items():
        if v is not None:
            try:
                out[k] = round(float(v), 3)
            except (TypeError, ValueError):
                out[k] = v
    out["data_as_of"] = result.get("data_as_of", "")
    out["is_stale"] = result.get("is_stale", False)
    return out


# ---------- 新闻：10 条 → ≤400 tokens ----------

BULL_WORDS = ["上涨", "利好", "增长", "突破", "超预期", "中标", "回购", "增持", "分红", "盈利", "获批", "新高"]
BEAR_WORDS = ["下跌", "利空", "下滑", "亏损", "减持", "质押", "处罚", "立案", "诉讼", "违规", "退市", "低迷"]


def label_sentiment(title: str) -> str:
    """规则级情绪标签（搜索模块情绪预聚合复用）。"""
    if any(w in title for w in BULL_WORDS):
        return "bullish"
    if any(w in title for w in BEAR_WORDS):
        return "bearish"
    return "neutral"


def compact_news(result: dict, max_items: int = 5) -> dict:
    """新闻 → 去重后 5 条 + 标题情绪标签 + 聚合情绪分。"""
    items = result.get("news") or result.get("items") or []
    seen: set[str] = set()
    deduped = []
    for it in items:
        title = str(it.get("title", "")).strip()
        if not title:
            continue
        key = title[:40]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
        if len(deduped) >= max_items:
            break
    labeled = [
        {"title": str(it.get("title", ""))[:60],
         "source": it.get("source", ""),
         "time": str(it.get("published_at", ""))[:16],
         "sentiment": label_sentiment(str(it.get("title", "")))}
        for it in deduped
    ]
    score_map = {"bullish": 1, "neutral": 0, "bearish": -1}
    agg = sum(score_map.get(x["sentiment"], 0) for x in labeled) / len(labeled) if labeled else 0.0
    return {
        "ok": bool(labeled),
        "kind": "news",
        "items": labeled,
        "total_raw": len(items),
        "kept": len(labeled),
        "aggregate_sentiment": round(agg, 2),
        "data_as_of": result.get("data_as_of", ""),
        "is_stale": result.get("is_stale", False),
    }


# ---------- 财务：→ ≤400 tokens ----------

def compact_financial(result: dict) -> dict:
    """三表关键项同比（营收/净利/毛利/经营现金流 + 同比）。"""
    rows = result.get("indicators") or result.get("data") or []
    keep = [
        ("营收", ["营业收入", "营业总收入"]),
        ("净利", ["净利润", "归母净利润"]),
        ("毛利", ["毛利润", "销售毛利率"]),
        ("经营现金流", ["经营活动产生的现金流量净额", "经营现金流净额"]),
    ]
    out: dict[str, Any] = {"ok": bool(rows), "kind": "financial", "items": []}
    latest = rows[0] if rows else {}
    for label, keys in keep:
        for k in keys:
            if k in latest:
                item: dict[str, Any] = {"metric": label, "value": latest[k]}
                for yk, ylabel in (("同比增长", "yoy"), ("同比增速", "yoy")):
                    if yk in latest:
                        item[ylabel] = latest[yk]
                        break
                out["items"].append(item)
                break
    out["period"] = latest.get("报告期") or latest.get("report_date", "")
    return out


# ---------- 回测：→ ≤200 tokens ----------

def compact_backtest(result: dict) -> dict:
    """回测 → 指标表（夏普/回撤/胜率/盈亏比/年化）。"""
    m = result.get("metrics") or {}
    keys = ["total_return", "annual_return", "sharpe", "max_drawdown",
            "win_rate", "profit_loss_ratio", "trade_count"]
    labels = {"total_return": "累计收益", "annual_return": "年化", "sharpe": "夏普",
              "max_drawdown": "最大回撤", "win_rate": "胜率",
              "profit_loss_ratio": "盈亏比", "trade_count": "交易次数"}
    out: dict[str, Any] = {"ok": bool(m), "kind": "backtest", "metrics": {}}
    for k in keys:
        if k in m:
            v = m[k]
            out["metrics"][labels[k]] = round(float(v), 4) if isinstance(v, (int, float)) else v
    if result.get("equity_curve"):
        eq = result["equity_curve"]
        out["curve_points"] = len(eq)
    return out


# ---------- 数据/分析/报告工具压缩（M6） ----------

def _compact_table(table: dict | None, limit: int = 20) -> dict:
    table = table or {}
    rows = table.get("rows") or []
    return {
        "columns": table.get("columns") or [],
        "rows": rows[:limit],
        "row_count": len(rows),
        "truncated": len(rows) > limit,
    }


def compact_catalog_search(result: dict) -> dict:
    items = result.get("items") or []
    keys = ("instrument_id", "symbol", "name", "asset_class", "exchange",
            "sector", "industry", "country", "listing_status")
    return {
        "ok": result.get("ok", True),
        "total": result.get("total", len(items)),
        "items": [{key: item.get(key) for key in keys} for item in items[:10]],
        "truncated": len(items) > 10,
        "hint": result.get("hint", ""),
    }


def compact_dataset_list(result: dict) -> dict:
    datasets = result.get("datasets") or []
    keys = ("dataset_id", "name", "table_name", "version", "rows", "columns", "sha256")
    return {
        "ok": result.get("ok", True),
        "count": result.get("count", len(datasets)),
        "datasets": [{key: d.get(key) for key in keys} for d in datasets[:20]],
        "truncated": len(datasets) > 20,
        "hint": result.get("hint", ""),
    }


def compact_dataset_profile(result: dict) -> dict:
    profile = result.get("profile") or []
    keys = ("name", "dtype", "null_count", "null_ratio", "distinct_count",
            "min", "max", "p25", "p50", "p75")
    schema = result.get("schema") or {}
    return {
        "ok": result.get("ok", True),
        "dataset_id": result.get("dataset_id"),
        "name": result.get("name"),
        "version": result.get("version"),
        "rows": result.get("rows"),
        "columns": result.get("columns"),
        "schema": dict(list(schema.items())[:50]),
        "schema_truncated": len(schema) > 50,
        "profiled_columns": result.get("profiled_columns"),
        "truncated_columns": result.get("truncated_columns"),
        "profile": [{key: col.get(key) for key in keys} for col in profile[:20]],
        "truncated": len(profile) > 20,
    }


def compact_sql(result: dict) -> dict:
    rows = result.get("rows") or []
    return {
        "ok": result.get("ok", True),
        "columns": result.get("columns") or [],
        "rows": rows[:20],
        "row_count": result.get("row_count", len(rows)),
        "truncated": bool(result.get("truncated")) or len(rows) > 20,
        "sql_hash": result.get("sql_hash"),
        "result_hash": result.get("result_hash"),
        "datasets": result.get("datasets") or [],
        "hint": result.get("hint", ""),
    }


def compact_analysis(result: dict) -> dict:
    data = result.get("data") or {}
    chart = result.get("chart") or {}
    summary = {key: value for key, value in data.items()
               if key not in ("table", "x", "series", "matrix", "items", "columns")}
    return {
        "ok": result.get("ok", True),
        "module": result.get("module"),
        "label": result.get("label"),
        "dataset": result.get("dataset"),
        "params": result.get("params"),
        "warnings": result.get("warnings") or [],
        "result_hash": result.get("result_hash"),
        "chart": {key: chart.get(key) for key in
                  ("chart_type", "title", "spec_hash", "result_hash")} if chart else None,
        "summary": summary,
        "table": _compact_table(data.get("table"), limit=20),
    }


def compact_report(result: dict) -> dict:
    blocks = result.get("blocks") or []
    return {
        "ok": result.get("ok", True),
        "report_id": result.get("report_id"),
        "title": result.get("title"),
        "spec_hash": result.get("spec_hash"),
        "files": result.get("files") or [],
        "datasets": result.get("datasets") or [],
        "sources": [{key: source.get(key) for key in
                     ("block_id", "block_title", "type", "result_hash",
                      "chart_spec_hash", "sql_hash", "dataset_sha256")}
                    for source in (result.get("sources") or [])],
        "blocks": [{key: block.get(key) for key in
                    ("id", "type", "title", "source")}
                   for block in blocks],
        "hint": result.get("hint", ""),
    }


# ---------- 分发 ----------

COMPACTORS = {
    "get_kline": compact_kline,
    "get_quote": compact_quote,
    "search_news": compact_news,
    "get_financial": compact_financial,
    "run_backtest": compact_backtest,
    "catalog_search": compact_catalog_search,
    "dataset_list": compact_dataset_list,
    "dataset_profile": compact_dataset_profile,
    "sql_query": compact_sql,
    "analysis_run": compact_analysis,
    "report_compose": compact_report,
}


def pack(tool_name: str, result: dict) -> dict:
    """进模型上下文前的统一压缩入口；未注册压缩器的工具原样返回（raw 档）。"""
    fn = COMPACTORS.get(tool_name)
    if fn is None:
        return result
    try:
        return fn(result)
    except Exception:
        # 压缩失败 → 原样返回（原始结果永远可信）
        return result
