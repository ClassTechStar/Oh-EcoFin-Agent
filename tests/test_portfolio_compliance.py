"""组合优化（PyPortfolioOpt 输入形态断言 / Ledoit-Wolf / 前沿缓存）+ 合规审查链。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.quant.compliance import (Portfolio, compliance_check,
                                      map_score_to_level)
from backend.quant.portfolio import PortfolioOptimizer, _assert_prices


def make_price_matrix(n_days=500, n_assets=5, seed=11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0003, 0.02, (n_days, n_assets))
    prices = 50 * np.cumprod(1 + rets, axis=0)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    return pd.DataFrame(prices, index=dates.strftime("%Y-%m-%d"),
                        columns=[f"S{i}" for i in range(n_assets)])


# ---------- 输入形态断言（避坑 #2） ----------

def test_assert_prices_rejects_returns():
    """PyPortfolioOpt 吃【价格】DataFrame，传收益率必错 → 封装层类型断言。"""
    prices = make_price_matrix()
    returns = prices.pct_change().dropna()
    with pytest.raises(TypeError, match="价格"):
        _assert_prices(returns)


def test_assert_prices_rejects_short_series():
    with pytest.raises(ValueError, match="过短"):
        _assert_prices(make_price_matrix(n_days=20))


def test_assert_prices_rejects_non_frame():
    with pytest.raises(TypeError):
        _assert_prices([1, 2, 3])


# ---------- 优化 ----------

def test_optimize_r4_max_sharpe():
    opt = PortfolioOptimizer()
    prices = make_price_matrix()
    r = opt.optimize(prices, "R4", equity_cap=0.45, single_cap=0.25)
    assert r.mode == "max_sharpe"
    total = sum(r.weights.values())
    assert 0.99 <= total <= 1.01
    assert all(w >= 0 for w in r.weights.values())
    assert r.sharpe > 0


def test_optimize_r3_min_volatility():
    opt = PortfolioOptimizer()
    prices = make_price_matrix(seed=12)
    r = opt.optimize(prices, "R3", equity_cap=0.35, single_cap=0.20)
    assert r.mode == "min_volatility"


def test_optimize_single_cap_respected():
    """单一资产上限约束生效。"""
    opt = PortfolioOptimizer()
    prices = make_price_matrix(seed=13)
    cap = 0.25
    r = opt.optimize(prices, "R4", equity_cap=0.45, single_cap=cap)
    assert max(r.weights.values()) <= cap + 1e-6


def test_frontier_cache_same_day_reuse():
    """有效前沿缓存：当日重复对话零重算。"""
    opt = PortfolioOptimizer()
    prices = make_price_matrix(seed=14)
    r1 = opt.efficient_frontier(prices, "R4", 0.45, 0.25)
    r2 = opt.efficient_frontier(prices, "R4", 0.45, 0.25)
    assert r1 is r2  # 缓存命中（同一对象）
    assert len(r1["frontier"]) >= 10
    assert "weights" in r1["point"]


def test_frontier_point_highlighted():
    opt = PortfolioOptimizer()
    prices = make_price_matrix(seed=15)
    out = opt.efficient_frontier(prices, "R5", 0.50, 0.30)
    assert out["point"]["level"] == "R5"


# ---------- 合规审查链（确定性代码，非 LLM） ----------

def test_compliance_equity_cap_violation():
    p = Portfolio(weights={"600519": 0.4, "000001": 0.1},
                  disclaimer="x" * 30, asset_types={})
    v = compliance_check(p, "R3")  # R3 权益上限 35%
    assert any(x.rule == "equity_cap" for x in v)


def test_compliance_single_cap_violation():
    p = Portfolio(weights={"600519": 0.45, "000001": 0.1},
                  disclaimer="x" * 30)
    v = compliance_check(p, "R4")  # 单一上限 25%
    assert any(x.rule == "single_cap" for x in v)


def test_compliance_missing_disclaimer():
    p = Portfolio(weights={"600519": 0.2, "000001": 0.2}, disclaimer="")
    v = compliance_check(p, "R3")
    assert any(x.rule == "disclaimer" for x in v)


def test_compliance_pass_case():
    p = Portfolio(weights={"600519": 0.2, "000001": 0.2, "511010": 0.2, "019547": 0.4},
                  disclaimer="本组合由教学演示智能体生成，仅供课程学习使用。",
                  defensive={"511010", "019547"})
    assert compliance_check(p, "R4") == []  # 空列表 = 通过


def test_compliance_bond_not_equity():
    """债券/基金不计入权益上限：R5 权益=50%，若债券被误算则 100% 必然违规。"""
    p = Portfolio(weights={"600519": 0.5, "511010": 0.5},  # 511010 国债
                  disclaimer="x" * 30)
    v = compliance_check(p, "R5")
    assert not any(x.rule == "equity_cap" for x in v)


def test_compliance_unknown_level():
    p = Portfolio(weights={"600519": 0.1}, disclaimer="x" * 30)
    v = compliance_check(p, "R9")
    assert any(x.rule == "risk_level" for x in v)


def test_violation_injection_three_cases():
    """违规注入用例 3 个（占比超限/单一超限/缺声明）。"""
    cases = [
        (Portfolio({"600519": 0.5}, "d" * 40, {}), "R3", "equity_cap"),
        (Portfolio({"600519": 0.5}, "d" * 40, {}), "R4", "single_cap"),
        (Portfolio({"600519": 0.2}, "", {}), "R3", "disclaimer"),
    ]
    for p, lv, rule in cases:
        assert any(x.rule == rule for x in compliance_check(p, lv))


# ---------- 测评分 → 等级映射 ----------

def test_score_mapping_r1_to_r5():
    assert map_score_to_level(5) == "R1"
    assert map_score_to_level(8) == "R1"
    assert map_score_to_level(9) == "R2"
    assert map_score_to_level(12) == "R2"
    assert map_score_to_level(13) == "R3"
    assert map_score_to_level(17) == "R3"
    assert map_score_to_level(18) == "R4"
    assert map_score_to_level(21) == "R4"
    assert map_score_to_level(22) == "R5"
    assert map_score_to_level(25) == "R5"
