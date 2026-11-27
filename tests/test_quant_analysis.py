"""Walk-forward 切折与衰减率 + 多因子（MAD/Z/IC/t统计量）+ 未来函数纪律。"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd
import pytest

from backend.quant.factors import (analyze_cross_section, composite_score,
                                   compute_factors, factor_ic_table, ic_t_stat,
                                   mad_clip, rank_ic, zscore)
from backend.quant.walkforward import make_folds, walk_forward


def test_make_folds_counts_and_disjoint():
    df = make_price_df(1100)  # 4 折滚动需 ≥ 3×step + train + oos = 1008 行
    folds = make_folds(df, max_folds=4)
    assert len(folds) == 4
    for f in folds:
        assert len(f.train_df) == 504
        assert len(f.oos_df) == 126
        # 训练窗与样本外不重叠
        assert f.train_df["date"].iloc[-1] < f.oos_df["date"].iloc[0]
    # 逐步滚动
    assert folds[0].train_df["date"].iloc[0] < folds[1].train_df["date"].iloc[0]


def make_price_df(days: int, seed: int = 3) -> pd.DataFrame:
    from tests.helpers import synthetic_ohlcv

    return synthetic_ohlcv(days, seed)


def test_walk_forward_decay_output():
    # 1100 日 → 2 折完整（每折 504 训练 + 126 样本外，滚动 step=126）
    df = make_price_df(1100)
    out = walk_forward(df, "600519", max_folds=2)
    assert out["ok"] is True
    assert out["n_folds"] == 2
    for f in out["folds"]:
        assert "is_sharpe" in f and "oos_sharpe" in f and "decay" in f
        # decay 落库时已 round(3)，与原始比值在 3 位小数下一致
        assert f["decay"] == pytest.approx(
            round(f["oos_sharpe"] / max(f["is_sharpe"], 1e-9), 3), abs=1e-9)
    # 样本内寻优夏普通常显著高于样本外（衰减叙事）
    assert out["interpretation"]


def test_walk_forward_insufficient_data(ohlcv):
    out = walk_forward(ohlcv.head(100), "600519", max_folds=4)
    assert out["ok"] is False or out["n_folds"] == 0


# ---------- 因子 ----------

def test_mad_clip_bounds_outliers():
    s = pd.Series([1, 2, 3, 4, 5, 100.0])  # 100 是离群值
    clipped = mad_clip(s)
    assert clipped.max() < 100.0
    assert (clipped.median() == s.median())


def test_zscore_standardizes():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    z = zscore(s)
    assert z.mean() == pytest.approx(0.0, abs=1e-9)
    assert z.std() == pytest.approx(1.0, rel=1e-9)
    # 常数列 → 全 0（不产生 NaN 噪声）
    z0 = zscore(pd.Series([3.0] * 5))
    assert z0.abs().sum() == 0


def test_rank_ic_range_and_sign():
    factor = pd.Series(np.linspace(1, 10, 50))
    strong = factor * 0.1 + np.random.default_rng(1).normal(0, 0.05, 50)  # 强正相关
    ic = rank_ic(factor, pd.Series(strong))
    assert ic > 0.8
    anti = -factor * 0.1
    ic2 = rank_ic(factor, pd.Series(anti))
    assert ic2 < -0.99


def test_ic_t_stat_significance():
    rng = np.random.default_rng(2)
    ics = pd.Series(rng.normal(0.02, 0.1, 36))   # 弱信号 → |t| < 3
    t = ic_t_stat(ics)
    assert abs(t) < 3
    ics_strong = pd.Series(rng.normal(0.10, 0.05, 36))  # 强信号 → |t| > 3
    t2 = ic_t_stat(ics_strong)
    assert t2 > 3.0


def test_compute_factors_uses_only_past_data():
    """未来函数纪律：因子计算只用 ≤t 数据（输入即历史窗口值）。"""
    panel = pd.DataFrame({
        "pe": [20.0, 30.0, 50.0, None], "roe": [0.1, 0.15, 0.05, 0.2],
        "close": [10.0, 20.0, 15.0, 8.0],
        "close_120d_ago": [9.0, 18.0, 14.0, 7.5],
        "close_60d_ago": [9.5, 19.0, 14.5, 7.8],
        "vol_60d": [0.02, 0.03, 0.01, 0.05],
    }, index=["A", "B", "C", "D"])
    factors = compute_factors(panel)
    assert factors.loc["A", "ep"] == pytest.approx(1 / 20.0)
    assert np.isnan(factors.loc["D", "ep"])  # pe 无效 → NaN（不计入）
    assert factors.loc["B", "momentum_120d"] == pytest.approx(20.0 / 18.0 - 1)


def test_composite_score_low_vol_negative_direction():
    panel = pd.DataFrame({
        "pe": [10.0, 20.0, 40.0], "roe": [0.2, 0.1, 0.05],
        "close": [10.0, 20.0, 30.0],
        "close_120d_ago": [10.0, 20.0, 30.0],
        "close_60d_ago": [10.0, 20.0, 30.0],
        "vol_60d": [0.01, 0.05, 0.10],  # 低波动 → 得分更高
    }, index=["A", "B", "C"])
    factors = compute_factors(panel)
    score = composite_score(factors)
    assert score.loc["A"] > score.loc["C"]  # 低波动 A 高于高波动 C


def test_analyze_cross_section_top20_and_ic():
    rng = np.random.default_rng(5)
    n = 100
    panel = pd.DataFrame({
        "pe": rng.uniform(5, 80, n), "roe": rng.uniform(0, 0.3, n),
        "close": rng.uniform(5, 100, n),
        "close_120d_ago": np.nan, "close_60d_ago": np.nan,
        "vol_60d": rng.uniform(0.01, 0.2, n),
    }, index=[f"S{i:03d}" for i in range(n)])
    panel["close_120d_ago"] = panel["close"] * rng.uniform(0.8, 1.2, n)
    panel["close_60d_ago"] = panel["close"] * rng.uniform(0.9, 1.1, n)
    fr = pd.Series(rng.normal(0, 0.1, n), index=panel.index)
    out = analyze_cross_section(panel, fr)
    assert out["ok"] is True
    assert out["universe_size"] == n
    assert len(out["top20"]) == 20
    assert out["ic_20d"] is None or -1 <= out["ic_20d"] <= 1


def test_factor_ic_table_full_pipeline():
    """IC 表决定因子选择（答辩 Q&A 答案就是这张表）。横截面 ≥50 只标的。"""
    from tests.helpers import synthetic_ohlcv

    n = 60
    symbols = [f"6{i:05d}" for i in range(n)]
    cols = {sym: synthetic_ohlcv(500, seed=zlib.crc32(sym.encode()) % 1000)
            .set_index("date")["close"].astype(float) for sym in symbols}
    price_wide = pd.DataFrame(cols).dropna()
    rng = np.random.default_rng(9)
    fundamentals = pd.DataFrame(
        {sym: {"pe": float(rng.uniform(5, 80)), "roe": float(rng.uniform(0, 0.3))}
         for sym in symbols}).T
    out = factor_ic_table(price_wide, fundamentals)
    assert out["ok"] is True
    assert out["periods"] >= 1
    for name, row in out["ic_table"].items():
        assert "ic_mean" in row and "t_stat" in row and "significant" in row
        assert row["significant"] == (abs(row["t_stat"]) > 2)
