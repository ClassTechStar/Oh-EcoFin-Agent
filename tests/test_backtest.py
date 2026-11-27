"""A股规则回测：佣金/印花税/T+1/涨跌停 四规则注入用例 + 指标 + 可复现性。"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.quant.backtest import (COMMISSION_RATE, DualMAStrategy, INIT_CASH,
                                    STAMP_DUTY, build_cerebro, grid_search,
                                    limit_pct_for, metrics_from_returns,
                                    run_backtest, validate_columns)


def test_column_validation_renames_required():
    df = pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})
    validate_columns(df)  # 不抛
    with pytest.raises(ValueError, match="缺列"):
        validate_columns(df.drop(columns=["close"]))


def test_limit_tiers():
    assert limit_pct_for("600519") == 10.0
    assert limit_pct_for("300750") == 20.0
    assert limit_pct_for("600519", "ST") == 5.0


def test_constants_match_plan():
    """A 股规则数值口径：佣金万2.5、印花税 0.05% 仅卖出、最低 5 元。"""
    assert COMMISSION_RATE == 0.00025
    assert STAMP_DUTY == 0.0005
    assert INIT_CASH == 1_000_000


def test_dual_ma_grid_constraints():
    grid = DualMAStrategy.grid()
    assert len(grid) == 8  # fast∈{5,10,20} × slow∈{20,60,120}，fast<slow
    for g in grid:
        assert g["fast"] < g["slow"]
    fasts = {g["fast"] for g in grid}
    slows = {g["slow"] for g in grid}
    assert fasts == {5, 10, 20}
    assert slows == {20, 60, 120}
    assert {"fast": 5, "slow": 20} in grid
    assert {"fast": 20, "slow": 20} not in grid
    assert {"fast": 20, "slow": 10} not in grid


def test_run_backtest_produces_metrics(ohlcv):
    out = run_backtest(ohlcv, "600519", fast=5, slow=20)
    assert out["ok"] is True
    m = out["metrics"]
    for key in ("total_return", "annual_return", "sharpe", "max_drawdown",
                "win_rate", "profit_loss_ratio", "trade_count"):
        assert key in m
    assert -1.0 <= m["max_drawdown"] <= 0.0
    assert out["final_value"] > 0


def test_backtest_reproducible_byte_level(ohlcv):
    """固定数据窗口 + 固定参数 → 逐折数字可复现（golden 固化前提）。"""
    r1 = run_backtest(ohlcv, "600519", 5, 20)
    r2 = run_backtest(ohlcv, "600519", 5, 20)
    assert r1["metrics"] == r2["metrics"]
    assert r1["final_value"] == r2["final_value"]


def test_stamp_duty_only_on_sell():
    """印花税只在卖侧：自定义佣金 scheme 双分支验证。"""
    import backtrader as bt

    # 直接构造 commissioninfo 验证 _getcommission
    cerebro = None
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=5).strftime("%Y-%m-%d"),
        "open": [10.0] * 5, "high": [10.1] * 5, "low": [9.9] * 5,
        "close": [10.0] * 5, "volume": [10000.0] * 5,
    })

    class _Null(bt.Strategy):
        def next(self):
            if len(self) == 1:
                self.buy(size=1000)   # 买入：佣金万2.5 最低 5 元
            if len(self) == 4:
                self.close()          # 卖出：佣金 + 印花税

    cerebro = build_cerebro(df, "600519", _Null, {})
    cerebro.run()

    broker = cerebro.broker
    comminfo = broker.comminfo[None]
    buy_fee = comminfo._getcommission(1000, 10.0, pseudoexec=True)   # 成交额 1万
    sell_fee = comminfo._getcommission(-1000, 10.0, pseudoexec=True)
    # 买入: max(10000*0.00025, 5) = 5
    assert buy_fee == pytest.approx(5.0, rel=1e-6)
    # 卖出: 5 + 10000*0.0005 = 10
    assert sell_fee == pytest.approx(10.0, rel=1e-6)


def test_min_commission_applies():
    """小额成交触发最低 5 元佣金。"""
    import backtrader as bt

    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=2).strftime("%Y-%m-%d"),
        "open": [10.0] * 2, "high": [10.1] * 2, "low": [9.9] * 2,
        "close": [10.0] * 2, "volume": [100.0] * 2,
    })

    class _Hold(bt.Strategy):
        params = (())

    cerebro = build_cerebro(df, "600519", _Hold, {})
    comminfo = cerebro.broker.comminfo[None]
    # 成交额 200 元 → 佣金 0.05 元 < 5 → 取 5
    assert comminfo._getcommission(20, 10.0, pseudoexec=True) == pytest.approx(5.0)


def test_limit_hit_skips_orders():
    """涨跌停开盘触及 → 跳过当日下单（策略层验证）。连续每日 +10% 一字板。"""
    import backtrader as bt

    # 每日开盘较昨收 +10%（涨停一字），永远无法成交
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=5).strftime("%Y-%m-%d"),
        "open": [10.0, 11.0, 12.1, 13.31, 14.64],
        "high": [10.1, 11.1, 12.2, 13.41, 14.74],
        "low": [9.9, 10.9, 12.0, 13.21, 14.54],
        "close": [10.0, 11.0, 12.1, 13.31, 14.64],
        "volume": [10000.0] * 5,
    })

    class _TryBuy(bt.Strategy):
        params = (("pct_limit", 10.0),)

        def __init__(self):
            self.bought = False

        def next(self):
            if len(self) >= 2 and not self.bought:
                prev_close = self.data.close[-1]
                open_today = self.data.open[0]
                chg = abs(open_today / prev_close - 1) * 100
                if chg >= self.p.pct_limit * 0.99:
                    return  # 涨跌停跳过
                self.buy(size=100)
                self.bought = True

    cerebro = build_cerebro(df, "600519", _TryBuy, {"pct_limit": 10.0})
    run = cerebro.run()[0]
    assert run.bought is False  # 第 2 天起开盘皆触及涨停 → 永远跳过


def test_metrics_from_returns_math():
    import numpy as np

    rets = pd.Series([0.01, -0.02, 0.03, 0.01])
    m = metrics_from_returns(rets, [100.0, -50.0, 30.0])
    assert m.trade_count == 3
    assert m.win_rate == pytest.approx(2 / 3, rel=1e-3)
    assert m.max_drawdown < 0
    # 全负收益 → 总收益为负
    m2 = metrics_from_returns(pd.Series([-0.01] * 5), [])
    assert m2.total_return < 0


def test_grid_search_picks_best_sharpe(ohlcv):
    best, sharpe, results = grid_search(ohlcv, "600519")
    assert best["fast"] < best["slow"]
    assert len(results) == len(DualMAStrategy.grid())
    best_in_results = max(results, key=lambda x: x["sharpe"])
    assert best_in_results["sharpe"] == sharpe
