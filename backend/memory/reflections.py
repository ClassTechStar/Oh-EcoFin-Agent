"""T+N 自动结算与反思生成（交易日历感知，仅收盘价口径）。"""

from __future__ import annotations

import logging

import pandas as pd

from backend.engine.prompts import REFLECTION_PROMPT
from backend.memory.decisions import DecisionStore, direction_ok

logger = logging.getLogger(__name__)


def settle_return(close_series: pd.Series, anchor_date: str, horizon_days: int,
                  calendar=None) -> tuple[float, str] | None:
    """计算 T+N 收盘价收益：close[t+N]/close[t] - 1。

    交易日历感知：N 个【交易日】（不足则返回 None 待后续结算）。
    返回 (收益率, 结算日) 或 None。
    """
    s = close_series.dropna()
    if s.empty:
        return None
    try:
        idx = list(s.index.astype(str))
        anchor = next(i for i, d in enumerate(idx) if d[:10] == str(anchor_date)[:10])
    except StopIteration:
        return None
    target = anchor + horizon_days
    if target >= len(s):
        return None
    ret = float(s.iloc[target] / s.iloc[anchor] - 1)
    return ret, str(s.index[target])[:10]


async def settle_pending(store: DecisionStore, get_close_fn, gateway,
                         calendar=None, max_reflections: int = 5) -> list[dict]:
    """扫描未结算决策 → 结算 → economy 模型生成 ≤80 字反思。

    get_close_fn(symbol, days) -> pd.Series(index=date, values=close)
    """
    settled_log = []
    for d in store.pending_decisions():
        try:
            horizon = int(d["horizon_days"] or 5)
            need_days = horizon + 10
            close = await get_close_fn(d["symbol"], need_days)
            if close is None or len(close) == 0:
                continue
            result = settle_return(close, d["data_as_of"], horizon)
            if result is None:
                continue
            actual_return, settled_day = result
            reflection = ""
            # 反思生成（economy 模型；watch 方向不判对错但仍生成教训）
            if gateway is not None:
                from backend.engine.prompts import DISCLAIMER  # noqa: F401

                prompt = REFLECTION_PROMPT.format(
                    n=horizon, symbol=d["symbol"], verdict=d["verdict"],
                    thesis=d["thesis_digest"][:100], actual_return=actual_return)
                try:
                    reflection = (await gateway.simple(
                        [{"role": "user", "content": prompt}], "economy",
                        role="reflection", max_tokens=120)).strip()[:120]
                except Exception:
                    logger.warning("反思生成失败，降级为规则文本")
                    reflection = (
                        f"判断{d['verdict']}，实际区间收益 {actual_return:+.2%}；"
                        + ("方向正确" if direction_ok(d["verdict"], actual_return)
                           else "方向错误，注意数据时效与情绪钝化"))
            store.mark_settled(d["id"], actual_return, reflection)
            settled_log.append({"id": d["id"], "symbol": d["symbol"],
                                "verdict": d["verdict"], "return": round(actual_return, 4),
                                "settled_day": settled_day, "reflection": reflection})
        except Exception:
            logger.exception("结算失败: decision #%s", d.get("id"))
    return settled_log


def history_block(history: list[dict]) -> str:
    """历史决策 → 注入分析 prompt 的文本块。"""
    if not history:
        return ""
    lines = ["【该标的的历史决策记忆（记得上次的教训）】"]
    for h in history[:3]:
        ret = h.get("settled")
        ret_str = f"{ret:+.2%}" if isinstance(ret, (int, float)) else "未结算"
        lines.append(
            f"- {str(h.get('data_as_of', ''))[:10]} 判断 {h.get('verdict')}，"
            f"依据: {h.get('thesis_digest', '')[:60]}，T+N 结算: {ret_str}"
            + (f"，教训: {h['reflection']}" if h.get("reflection") else ""))
    return "\n".join(lines)
