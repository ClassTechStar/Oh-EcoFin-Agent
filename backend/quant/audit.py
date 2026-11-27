"""前视偏差（look-ahead bias）截断检验 —— 方法级审计门禁。

原理（移植自 FinancialAI-Agent `core/backtest/audit.py` 的截断检验思路）：
    若策略在 t 日只使用 ≤t 的数据，那么「cut 之后还有没有数据」不应影响它已经
    做出的决策。于是：
        1) 用完整区间跑一遍，记录 cut 日（含）之前的全部成交与权益；
        2) 把数据截断到 cut 日再跑一遍；
        3) 两者在 cut 之前必须逐笔一致。
    一旦不一致，说明某个信号偷看了未来（如用全样本 max 归一、shift(-1)、
    center=True 的滚动窗口）。

为什么优于静态扫描：
- 静态扫描只能匹配 `shift(-1)` 之类字面量，绕过方式极多；
- 截断检验是对**行为**的断言，与实现写法无关，且对离线 fixture 与真实行情同样有效。

局限（诚实声明）：
- 只能发现「影响成交」的泄漏；若泄漏仅影响未触发交易的中间变量，需配合代码评审；
- 需要策略在截断后仍能产生足够成交，否则标记 skipped 而非 passed。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from backend.quant.backtest import run_backtest

logger = logging.getLogger(__name__)

#: 默认在窗口 70% 处截断：太靠前预热不足，太靠后样本太少
DEFAULT_CUT_RATIO = 0.7

#: 权益比对容差（元）。同一决策路径下浮点误差应远小于该值
EQUITY_TOLERANCE = 0.01

Runner = Callable[[pd.DataFrame], dict]


@dataclass
class LeakReport:
    """单个「标的 × 策略」的截断检验结果。"""

    symbol: str
    strategy: str
    cut_date: str
    passed: bool
    fills_before_cut: int = 0
    equity_points_compared: int = 0
    leaks: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "cut_date": self.cut_date,
            "passed": self.passed,
            "fills_before_cut": self.fills_before_cut,
            "equity_points_compared": self.equity_points_compared,
            "leaks": self.leaks,
            "skipped_reason": self.skipped_reason,
        }


def _fill_key(f: dict) -> tuple:
    return (
        str(f.get("date")),
        str(f.get("side")),
        round(float(f.get("price") or 0.0), 4),
        int(f.get("size") or 0),
    )


def _pivot_date(df: pd.DataFrame, ratio: float) -> str | None:
    n = len(df)
    if n < 80:
        return None
    idx = min(max(int(n * ratio), 40), n - 20)
    return str(df["date"].iloc[idx])[:10]


def check_strategy(
    df: pd.DataFrame,
    symbol: str,
    *,
    strategy: str = "dual_ma",
    cut_ratio: float = DEFAULT_CUT_RATIO,
    runner: Runner | None = None,
) -> LeakReport:
    """对单个策略执行截断检验。

    Args:
        df: 已排序的日线数据（date/open/high/low/close/volume）
        symbol: 标的代码（仅用于报告）
        strategy: 策略标识（仅用于报告）
        cut_ratio: 截断位置比例
        runner: 回测执行器；默认使用 run_backtest（双均线）
    """
    runner = runner or (lambda d: run_backtest(d, symbol))

    if df is None or df.empty:
        return LeakReport(symbol, strategy, "", True, skipped_reason="数据为空")
    df = df.sort_values("date").reset_index(drop=True)

    cut_date = _pivot_date(df, cut_ratio)
    if cut_date is None:
        return LeakReport(symbol, strategy, "", True,
                          skipped_reason=f"样本不足以截断: {len(df)} 根")

    full = runner(df)
    if not full.get("ok"):
        return LeakReport(symbol, strategy, cut_date, True,
                          skipped_reason=f"完整区间回测失败: {full.get('reason', '')}")

    truncated_df = df[df["date"].astype(str).str.slice(0, 10) <= cut_date].copy()
    truncated = runner(truncated_df)
    if not truncated.get("ok"):
        return LeakReport(symbol, strategy, cut_date, True,
                          skipped_reason=f"截断区间回测失败: {truncated.get('reason', '')}")

    full_fills = [_fill_key(f) for f in full.get("executions", [])
                  if str(f.get("date")) <= cut_date]
    trunc_fills = [_fill_key(f) for f in truncated.get("executions", [])
                   if str(f.get("date")) <= cut_date]

    leaks: list[str] = []
    if full_fills != trunc_fills:
        n = max(len(full_fills), len(trunc_fills))
        for i in range(n):
            a = full_fills[i] if i < len(full_fills) else None
            b = trunc_fills[i] if i < len(trunc_fills) else None
            if a != b:
                leaks.append(f"成交序列第 {i + 1} 笔不一致：完整区间={a} 截断区间={b}")
                if len(leaks) >= 5:  # 只留前 5 条，避免报告膨胀
                    break

    # 权益曲线同口径比对（同一日期点）
    full_eq = {p["date"]: float(p["value"]) for p in full.get("equity_curve", [])
               if str(p.get("date")) <= cut_date}
    trunc_eq = {p["date"]: float(p["value"]) for p in truncated.get("equity_curve", [])
                if str(p.get("date")) <= cut_date}
    compared = 0
    for d, v_full in full_eq.items():
        if d in trunc_eq:
            compared += 1
            if abs(v_full - trunc_eq[d]) > EQUITY_TOLERANCE:
                leaks.append(f"权益曲线在 {d} 不一致：{v_full:.2f} vs {trunc_eq[d]:.2f}")
                if len(leaks) >= 5:
                    break

    if not trunc_fills and not full_fills:
        return LeakReport(symbol, strategy, cut_date, True, 0, compared,
                          skipped_reason="截断前无成交，检验无判别力")

    return LeakReport(
        symbol=symbol, strategy=strategy, cut_date=cut_date,
        passed=not leaks, fills_before_cut=len(full_fills),
        equity_points_compared=compared, leaks=leaks,
    )


def check_many(
    cases: list[tuple[pd.DataFrame, str, dict]],
    *,
    cut_ratio: float = DEFAULT_CUT_RATIO,
) -> list[LeakReport]:
    """批量检验：cases = [(df, symbol, runner_kwargs)]。

    runner_kwargs 会传给 run_backtest（如 fast/slow）。
    """
    reports = []
    for df, symbol, kwargs in cases:
        runner = (lambda d, kw=kwargs, s=symbol: run_backtest(d, s, **kw))
        reports.append(check_strategy(df, symbol, cut_ratio=cut_ratio, runner=runner))
    return reports


def summarize(reports: list[LeakReport]) -> str:
    """生成人类可读摘要（CI 日志用）。"""
    total = len(reports)
    passed = sum(1 for r in reports if r.passed)
    skipped = sum(1 for r in reports if r.skipped_reason and r.passed)
    lines = [f"截断检验：{passed}/{total} 通过（其中 {skipped} 项跳过）"]
    for r in reports:
        mark = "PASS" if r.passed else "LEAK"
        detail = f"，跳过原因: {r.skipped_reason}" if r.skipped_reason else ""
        lines.append(
            f"  [{mark}] {r.symbol} / {r.strategy} cut={r.cut_date} "
            f"fills={r.fills_before_cut} equity_pts={r.equity_points_compared}{detail}"
        )
        for leak in r.leaks:
            lines.append(f"        - {leak}")
    return "\n".join(lines)
