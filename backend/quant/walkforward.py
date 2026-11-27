"""Walk-forward 滚动验证：2 年训练 → 6 个月样本外 → 滚动 4 折，逐折衰减率 + bootstrap 置信区间。

报告叙事：样本内夏普动辄 2.0+，样本外 0.x 甚至为负——
"为什么不能直接用回测最优参数上实盘"的教科书答案。
固定数据窗口 + 固定参数网格 → 逐折数字可复现（golden 固化）。
置信区间：样本外日收益 bootstrap 重采样（N=1000，确定性种子）→ Sharpe 95% CI——
"样本外 0.x vs 样本内 2.0"的差距是否显著，一张带误差棒的图说清。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from backend.quant.backtest import grid_search, run_backtest

logger = logging.getLogger(__name__)

TRAIN_DAYS = 504   # 2 年训练
OOS_DAYS = 126     # 6 个月样本外
STEP_DAYS = 126    # 滚动步长
BOOTSTRAP_N = 1000
CI_LEVEL = 0.95
_BOOTSTRAP_SEED = 42  # 确定性 bootstrap（可复现纪律）


@dataclass
class Fold:
    fold_id: int
    train_df: pd.DataFrame
    oos_df: pd.DataFrame


@dataclass
class FoldResult:
    fold_id: int
    best_params: dict
    is_sharpe: float        # 样本内最优夏普
    oos_sharpe: float       # 样本外夏普
    oos_sharpe_ci: tuple[float, float]  # 样本外夏普 bootstrap 95% CI
    decay: float            # oos / is 衰减率
    oos_return: float


def sharpe_of(returns: pd.Series | np.ndarray) -> float:
    """年化夏普（244 交易日口径，与 backtest.metrics_from_returns 同源）。"""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * (244 ** 0.5))


def bootstrap_sharpe_ci(returns: pd.Series | np.ndarray,
                        n_boot: int = BOOTSTRAP_N,
                        level: float = CI_LEVEL,
                        seed: int = _BOOTSTRAP_SEED) -> tuple[float, float]:
    """样本外日收益 bootstrap 重采样 → Sharpe 置信区间（确定性种子，逐折可复现）。"""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 20 or r.std() == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(r), size=(n_boot, len(r)))
    samples = r[idx]
    means = samples.mean(axis=1)
    stds = samples.std(axis=1)
    sharpes = np.zeros(n_boot)
    ok = stds > 0
    sharpes[ok] = means[ok] / stds[ok] * (244 ** 0.5)
    alpha = (1 - level) / 2
    lo, hi = np.quantile(sharpes, [alpha, 1 - alpha])
    return (round(float(lo), 3), round(float(hi), 3))


def make_folds(df: pd.DataFrame, train: int = TRAIN_DAYS, oos: int = OOS_DAYS,
               step: int = STEP_DAYS, max_folds: int = 4) -> list[Fold]:
    """滚动切折：第 i 折训练窗 [i*step, i*step+train)，样本外紧随其后。"""
    df = df.sort_values("date").reset_index(drop=True)
    folds = []
    fold_id = 0
    start = 0
    while start + train + oos <= len(df) and fold_id < max_folds:
        train_df = df.iloc[start:start + train].copy()
        oos_df = df.iloc[start + train:start + train + oos].copy()
        folds.append(Fold(fold_id, train_df, oos_df))
        fold_id += 1
        start += step
    return folds


def walk_forward(df: pd.DataFrame, symbol: str, max_folds: int = 4) -> dict:
    """全流程：逐折寻优 → 样本外执行 → 衰减率 + bootstrap 置信区间。同步函数（调用方进线程池）。"""
    folds = make_folds(df, max_folds=max_folds)
    results: list[FoldResult] = []
    for f in folds:
        best_params, is_sharpe, _ = grid_search(f.train_df, symbol)
        oos = run_backtest(f.oos_df, symbol, **best_params)
        oos_sharpe = oos["metrics"]["sharpe"]
        ci = bootstrap_sharpe_ci(oos.get("daily_returns") or [])
        decay = oos_sharpe / max(is_sharpe, 1e-9)
        results.append(FoldResult(
            fold_id=f.fold_id, best_params=best_params, is_sharpe=is_sharpe,
            oos_sharpe=oos_sharpe, oos_sharpe_ci=ci, decay=round(decay, 3),
            oos_return=oos["metrics"]["total_return"],
        ))
    avg_decay = sum(r.decay for r in results) / len(results) if results else 0.0
    # 跨折 CI 汇总：CI 下界均值——"最差情形下的样本外夏普"（对齐衰减叙事）
    ci_lo = round(sum(r.oos_sharpe_ci[0] for r in results) / len(results), 3) if results else 0.0
    ci_hi = round(sum(r.oos_sharpe_ci[1] for r in results) / len(results), 3) if results else 0.0
    return {
        "ok": bool(results), "symbol": symbol,
        "folds": [r.__dict__ for r in results],
        "avg_decay": round(avg_decay, 3),
        "n_folds": len(results),
        "oos_sharpe_ci": [ci_lo, ci_hi],
        "interpretation": (
            f"样本外夏普相对样本内平均衰减至 {avg_decay:.0%}"
            f"（bootstrap 95% CI [{ci_lo}, {ci_hi}]）——样本内最优参数不能直接实盘"
            if results else "数据不足，无法切折"
        ),
    }
