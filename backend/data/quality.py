"""数据质检与新鲜度协议：停牌/涨跌停/缺列 NaN 三查 + data_as_of / is_stale 强制携带。

涨跌停分档：主板 ±10%（pct ≥ 9.9 判定）、创业板/科创板 ±20%（19.9）、ST ±5%（4.9）。
"""

from __future__ import annotations

from typing import Any

# 板块档位（绝对涨跌幅上限）
LIMIT_TIERS = {
    "main": 10.0,     # 主板（60/00 开头）
    "growth": 20.0,   # 创业板（30 开头）/ 科创板（68 开头）
    "st": 5.0,        # ST 股（名称含 ST）
}


def classify_board(symbol: str, name: str = "") -> str:
    sym = str(symbol).strip()
    if "ST" in name.upper() or "ST" in sym.upper():
        return "st"
    if sym.startswith(("30", "68")):
        return "growth"
    return "main"


def limit_threshold(symbol: str, name: str = "") -> float:
    tier = classify_board(symbol, name)
    return LIMIT_TIERS[tier]


def check_suspended(row: dict[str, Any] | None) -> bool:
    """停牌：成交量为 0 或快照行缺失。"""
    if row is None:
        return True
    vol = row.get("volume") or row.get("成交量")
    try:
        return vol is None or float(vol) == 0
    except (TypeError, ValueError):
        return True


def check_limit_hit(symbol: str, pct_chg: float | None, name: str = "") -> str | None:
    """涨跌停判定：绝对值触及阈值 × 0.99（9.9/19.9/4.9 口径）。返回档位标签或 None。"""
    if pct_chg is None:
        return None
    try:
        pct = abs(float(pct_chg))
    except (TypeError, ValueError):
        return None
    thr = limit_threshold(symbol, name)
    if pct >= thr * 0.99:
        return f"limit_hit:{thr:.0f}%"
    return None


def validate_snapshot_df(df) -> list[str]:
    """快照列完整性检查：返回缺失关键列列表（空列表 = 通过）。"""
    required = ["代码", "名称", "最新价", "涨跌幅", "成交量"]
    if df is None:
        return required
    missing = [c for c in required if c not in getattr(df, "columns", [])]
    return missing


def freshness(data_as_of: str, is_stale: bool) -> dict[str, Any]:
    """新鲜度徽章三档：🟢 盘中<90s / 🟡 盘中>90s 或盘后当日 / 🔴 隔日。"""
    from datetime import datetime, timedelta, timezone

    level = "red"
    if data_as_of and not is_stale:
        try:
            ts = datetime.fromisoformat(data_as_of)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
            age = (datetime.now(timezone(timedelta(hours=8))) - ts).total_seconds()
            if age < 90:
                level = "green"
            elif age < 24 * 3600:
                level = "yellow"
        except ValueError:
            pass
    return {"data_as_of": data_as_of, "is_stale": is_stale, "freshness": level}
