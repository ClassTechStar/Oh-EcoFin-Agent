"""截断检验（前视偏差审计）测试。

关键验收：
1. 因果策略（双均线，信号 t 收盘算、t+1 开盘成交）必须通过；
2. 使用全样本统计量的泄漏策略必须被抓出；
3. 无成交时标注 skipped 而非假装通过；
4. 报告字段齐全、可序列化。
"""

from __future__ import annotations

import pandas as pd

from backend.quant.audit import LeakReport, check_many, check_strategy, summarize
from backend.quant.backtest import run_backtest
from backend.quant.fixtures import synthetic_ohlcv


def causal_runner(df: pd.DataFrame) -> dict:
    """因果策略：双均线（只用过去数据）。"""
    return run_backtest(df, "600519", fast=5, slow=20)


def test_causal_strategy_passes():
    df = synthetic_ohlcv(900, seed=21)
    report = check_strategy(df, "600519", strategy="dual_ma", runner=causal_runner)
    assert report.passed is True, report.leaks
    assert report.fills_before_cut > 0, "样本应产生截断前成交，否则检验无判别力"
    assert report.equity_points_compared > 0


def test_leaky_strategy_detected():
    """典型泄漏：用全样本均值/标准差做 z-score（截断后统计量改变 → 决策改变）。"""

    def leaky_runner(df: pd.DataFrame) -> dict:
        mean = float(df["close"].mean())      # ← 全样本统计量：泄漏源
        std = float(df["close"].std()) or 1.0
        executions, equity = [], []
        for i in range(len(df)):
            date = str(df["date"].iloc[i])[:10]
            close = float(df["close"].iloc[i])
            z = (close - mean) / std
            if z < -1.0:
                side = "buy"
            elif z > 1.0:
                side = "sell"
            else:
                side = None
            if side:
                executions.append({"date": date, "side": side,
                                   "price": round(close, 4), "size": 100,
                                   "commission": 0.0})
            equity.append({"date": date, "value": round(close, 2)})
        return {"ok": True, "executions": executions, "equity_curve": equity}

    df = synthetic_ohlcv(900, seed=22)
    report = check_strategy(df, "600519", strategy="leaky_zscore", runner=leaky_runner)
    assert report.passed is False
    assert report.leaks, "必须给出不一致明细"
    assert any("成交序列" in x or "权益曲线" in x for x in report.leaks)


def test_no_fills_marks_skipped_not_silent_pass():
    def idle_runner(df: pd.DataFrame) -> dict:
        return {"ok": True, "executions": [], "equity_curve": []}

    df = synthetic_ohlcv(400, seed=23)
    report = check_strategy(df, "600519", runner=idle_runner)
    assert report.passed is True
    assert "无成交" in report.skipped_reason


def test_short_sample_skipped():
    df = synthetic_ohlcv(60, seed=24)
    report = check_strategy(df, "600519", runner=causal_runner)
    assert report.passed is True
    assert report.skipped_reason and "样本不足" in report.skipped_reason


def test_failed_backtest_skipped():
    def broken_runner(df: pd.DataFrame) -> dict:
        return {"ok": False, "reason": "数据源不可用"}

    df = synthetic_ohlcv(400, seed=25)
    report = check_strategy(df, "600519", runner=broken_runner)
    assert report.passed is True
    assert "回测失败" in report.skipped_reason


def test_report_serializable_and_summary():
    df = synthetic_ohlcv(900, seed=26)
    report = check_strategy(df, "600519", runner=causal_runner)
    payload = report.to_dict()
    assert set(payload) >= {"symbol", "strategy", "cut_date", "passed",
                            "fills_before_cut", "equity_points_compared",
                            "leaks", "skipped_reason"}
    text = summarize([report])
    assert "截断检验" in text and "600519" in text


def test_check_many_with_params():
    cases = [
        (synthetic_ohlcv(900, seed=31), "600519", {"fast": 5, "slow": 20}),
        (synthetic_ohlcv(900, seed=32), "000001", {"fast": 10, "slow": 60}),
    ]
    reports = check_many(cases)
    assert len(reports) == 2
    assert all(isinstance(r, LeakReport) for r in reports)
    assert all(r.passed for r in reports), [r.leaks for r in reports]


def test_cut_ratio_affects_cut_date():
    df = synthetic_ohlcv(900, seed=33)
    early = check_strategy(df, "600519", runner=causal_runner, cut_ratio=0.5)
    late = check_strategy(df, "600519", runner=causal_runner, cut_ratio=0.8)
    assert early.cut_date < late.cut_date
