"""Markowitz 组合优化（PyPortfolioOpt + Ledoit-Wolf 收缩协方差）。

避坑铁律：
1. mean_historical_return 吃【价格】DataFrame（内部算收益率），传收益率必错 → 封装层类型断言；
2. Ledoit-Wolf 收缩协方差防小样本病态（手写必翻车，禁手写）；
3. weight_bounds + 单一资产上限约束按风险等级动态注入。
有效前沿缓存：键 =（标的池 hash + 交易日 + 风险档），当日重复对话零重算。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS = 244
RISK_FREE = 0.015


def _assert_prices(prices: pd.DataFrame) -> None:
    """类型断言：必须是价格 DataFrame（宽表，index=日期）而非收益率。"""
    if not isinstance(prices, pd.DataFrame):
        raise TypeError("PyPortfolioOpt 需要【价格】DataFrame，收到非 DataFrame")
    if prices.shape[0] < 30:
        raise ValueError(f"价格序列过短: {prices.shape[0]} 行（需 ≥30 个交易日）")
    if prices.abs().max().max() is not None and float(prices.abs().max().max()) < 0.5:
        raise TypeError("输入疑似收益率序列（数值过小）；PyPortfolioOpt 需要价格宽表")


@dataclass
class PortfolioResult:
    weights: dict[str, float]
    expected_return: float
    volatility: float
    sharpe: float
    level: str
    mode: str  # max_sharpe | min_volatility


class PortfolioOptimizer:
    def __init__(self):
        self._frontier_cache: dict[str, object] = {}

    def optimize(self, prices: pd.DataFrame, level: str,
                 equity_cap: float, single_cap: float,
                 equity_symbols: list[str] | None = None) -> PortfolioResult:
        """单点优化：R4+ 走 max_sharpe，否则 min_volatility。

        equity_symbols: 权益类标的列表（受 equity_cap 约束，近似为对每个权益标的
        加 weight ≤ equity_cap 约束——课程级简化，报告注明）。
        """
        from pypfopt import EfficientFrontier, expected_returns, risk_models

        _assert_prices(prices)
        mu = expected_returns.mean_historical_return(prices, frequency=TRADING_DAYS)
        S = risk_models.CovarianceShrinkage(prices, returns_data=False).ledoit_wolf()

        ef = EfficientFrontier(mu, S, weight_bounds=(0, max(equity_cap, single_cap)))
        # 单一资产上限
        tickers = list(prices.columns)
        equity_set = set(equity_symbols or tickers)
        for i, t in enumerate(tickers):
            if t in equity_set:
                ef.add_constraint(lambda w, i=i: w[i] <= single_cap)
            else:
                ef.add_constraint(lambda w, i=i: w[i] <= 1.0)

        if level in ("R4", "R5"):
            ef.max_sharpe(risk_free_rate=RISK_FREE)
            mode = "max_sharpe"
        else:
            ef.min_volatility()
            mode = "min_volatility"
        cleaned = ef.clean_weights(cutoff=0.005)
        ret, vol, sharpe = ef.portfolio_performance(risk_free_rate=RISK_FREE)
        weights = {t: round(w, 4) for t, w in cleaned.items() if w > 0}
        return PortfolioResult(weights, round(ret, 4), round(vol, 4),
                               round(sharpe, 3), level, mode)

    def efficient_frontier(self, prices: pd.DataFrame, level: str,
                           equity_cap: float, single_cap: float,
                           equity_symbols: list[str] | None = None,
                           n_points: int = 30) -> dict:
        """有效前沿曲线数据 + 单点组合；带交易日缓存键。"""
        from pypfopt import EfficientFrontier, expected_returns, risk_models

        cache_key = self._cache_key(prices, level)
        cached = self._frontier_cache.get(cache_key)
        if cached is not None:
            return cached

        _assert_prices(prices)
        mu = expected_returns.mean_historical_return(prices, frequency=TRADING_DAYS)
        S = risk_models.CovarianceShrinkage(prices, returns_data=False).ledoit_wolf()

        ef = EfficientFrontier(mu, S, weight_bounds=(0, max(equity_cap, single_cap)))
        tickers = list(prices.columns)
        equity_set = set(equity_symbols or tickers)
        for i, t in enumerate(tickers):
            if t in equity_set:
                ef.add_constraint(lambda w, i=i: w[i] <= single_cap)

        # 沿前沿采样：对多个目标波动求解最小收益/最大收益
        min_ret = float(mu.min())
        max_ret = float(mu.max())
        points = []
        for target in [min_ret + (max_ret - min_ret) * i / n_points for i in range(n_points + 1)]:
            try:
                ef2 = EfficientFrontier(mu, S, weight_bounds=(0, max(equity_cap, single_cap)))
                for i, t in enumerate(tickers):
                    if t in equity_set:
                        ef2.add_constraint(lambda w, i=i: w[i] <= single_cap)
                ef2.efficient_return(target_return=target)
                ret, vol, sharpe = ef2.portfolio_performance(risk_free_rate=RISK_FREE)
                points.append({"ret": round(ret, 4), "vol": round(vol, 4),
                               "sharpe": round(sharpe, 3)})
            except Exception:
                continue

        point = self.optimize(prices, level, equity_cap, single_cap, equity_symbols)
        result = {
            "ok": bool(points),
            "frontier": points,
            "point": {"weights": point.weights, "ret": point.expected_return,
                      "vol": point.volatility, "sharpe": point.sharpe,
                      "mode": point.mode, "level": level},
            "tickers": tickers,
        }
        self._frontier_cache[cache_key] = result
        return result

    def _cache_key(self, prices: pd.DataFrame, level: str) -> str:
        from backend.data.calendar import now_cst

        day = now_cst().strftime("%Y-%m-%d")
        pool_hash = hashlib.sha1(
            (",".join(prices.columns) + f"{prices.shape[0]}{prices.shape[1]}").encode()
        ).hexdigest()[:12]
        return f"{day}:{pool_hash}:{level}"
