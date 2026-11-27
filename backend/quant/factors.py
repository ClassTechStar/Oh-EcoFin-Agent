"""多因子选股：四因子横截面 + MAD 去极值 + Z 标准化 + 等权合成 + rank IC 验证。

因子（数据驱动，IC 表决定进 composite 的因子与权重）：
  价值: EP(TTM)=1/PE · 质量: ROE · 动量: 120日收益率 · 低波动: 60日 std
处理链: MAD 去极值（|x-med|>3×MAD → clip）→ Z 标准化（截面）→ 等权合成 → Top20
验证: rank IC（Spearman，因子值 vs 未来20日收益）→ IC 序列 t 统计量（|t|>2 显著）
      + IC 衰减曲线（1/5/10/20/60 日）

未来函数纪律：因子计算只用 ≤t 数据；"未来收益"仅用于评估不进入信号。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)

FACTOR_NAMES = ["ep", "roe", "momentum_120d", "vol_60d"]
TOP_N = 20
IC_HORIZONS = (1, 5, 10, 20, 60)


def mad_clip(series: pd.Series, n_mad: float = 3.0) -> pd.Series:
    """MAD 去极值：|x-med| > n×MAD → clip 到边界。"""
    med = series.median()
    mad = (series - med).abs().median()
    if mad == 0 or np.isnan(mad):
        return series
    lo, hi = med - n_mad * mad, med + n_mad * mad
    return series.clip(lo, hi)


def zscore(series: pd.Series) -> pd.Series:
    """截面 Z 标准化。"""
    std = series.std()
    if std == 0 or np.isnan(std):
        return series * 0
    return (series - series.mean()) / std


def compute_factors(panel: pd.DataFrame) -> pd.DataFrame:
    """横截面因子值。

    panel: DataFrame(index=股票代码, columns 至少含 [pe, roe, close, close_120d_ago, close_60d_ago])
      - close_120d_ago / close_60d_ago 为 t 日往回看的历史收盘（只用 ≤t 数据）
    """
    out = pd.DataFrame(index=panel.index)
    pe = pd.to_numeric(panel.get("pe"), errors="coerce")
    out["ep"] = np.where(pe > 0, 1.0 / pe, np.nan)
    out["roe"] = pd.to_numeric(panel.get("roe"), errors="coerce")
    close = pd.to_numeric(panel["close"], errors="coerce")
    base120 = pd.to_numeric(panel.get("close_120d_ago"), errors="coerce")
    out["momentum_120d"] = np.where(base120 > 0, close / base120 - 1, np.nan)
    out["vol_60d"] = pd.to_numeric(panel.get("vol_60d"), errors="coerce")
    return out


def composite_score(factors: pd.DataFrame, weights: dict[str, float] | None = None) -> pd.Series:
    """MAD → Z → 等权(或加权)合成。

    低波动因子方向取负（波动越低分越高）。
    """
    weights = weights or dict.fromkeys(FACTOR_NAMES, 0.25)
    total = pd.Series(0.0, index=factors.index)
    weight_sum = 0.0
    for name, w in weights.items():
        if name not in factors.columns:
            continue
        col = mad_clip(factors[name].dropna())
        z = zscore(col)
        if name == "vol_60d":
            z = -z
        total = total.add(z.fillna(0) * w, axis=0)
        weight_sum += w
    return total / weight_sum if weight_sum else total


def rank_ic(factor_values: pd.Series, forward_returns: pd.Series) -> float:
    """Spearman rank IC（因子值 vs 未来收益）。"""
    joined = pd.concat([factor_values, forward_returns], axis=1, keys=["f", "r"]).dropna()
    if len(joined) < 10:
        return float("nan")
    rho, _ = stats.spearmanr(joined["f"], joined["r"])
    return float(rho)


def ic_t_stat(ic_series: pd.Series) -> float:
    """IC 序列 t 统计量（|t|>2 即显著）。"""
    s = ic_series.dropna()
    if len(s) < 3 or s.std() == 0:
        return 0.0
    return float(s.mean() / s.std() * (len(s) ** 0.5))


def ic_decay(factor_series_by_date: dict[str, pd.Series],
             returns_by_horizon: dict[str, dict[int, pd.Series]]) -> dict[int, float]:
    """IC 衰减曲线：1/5/10/20/60 日各档平均 IC。"""
    out = {}
    for h in IC_HORIZONS:
        ics = []
        for date_key, factor_values in factor_series_by_date.items():
            fr = returns_by_horizon.get(date_key, {}).get(h)
            if fr is not None:
                ics.append(rank_ic(factor_values, fr))
        s = pd.Series(ics).dropna()
        out[h] = round(float(s.mean()), 4) if len(s) else float("nan")
    return out


def analyze_cross_section(panel: pd.DataFrame, forward_return_20d: pd.Series,
                          weights: dict[str, float] | None = None) -> dict:
    """单横截面全流程：因子 → 合成 → Top20 → 与未来 20 日收益的 IC。"""
    factors = compute_factors(panel)
    score = composite_score(factors, weights)
    valid = score.dropna().sort_values(ascending=False)
    top20 = list(valid.head(TOP_N).index)
    ic = rank_ic(score, forward_return_20d)
    return {
        "ok": True,
        "universe_size": int(panel.shape[0]),
        "top20": top20,
        "ic_20d": None if np.isnan(ic) else round(ic, 4),
        "score_stats": {"mean": round(float(valid.mean()), 4),
                        "std": round(float(valid.std()), 4)},
    }


def factor_ic_table(price_wide: pd.DataFrame, fundamentals: pd.DataFrame) -> dict:
    """多期 IC 表：月度横截面 × 未来 20 日收益 → t 统计量（数据驱动因子选择）。

    price_wide: index=date, columns=symbols 的收盘价矩阵（后复权）。
    fundamentals: index=symbol, columns=[pe, roe] 的最新截面值（演示口径）。
    """
    dates = price_wide.index
    ret20 = price_wide.pct_change(20).shift(-20)  # 未来收益仅用于评估
    ic_rows = []
    step = max(len(dates) // 12, 1)  # 月度采样
    for i in range(0, len(dates) - 20, step):
        t = dates[i]
        row = {}
        close_t = price_wide.loc[t]
        base120 = price_wide.shift(120).loc[t]
        base60 = price_wide.shift(60).loc[t]
        vol60 = price_wide.pct_change().rolling(60).std().loc[t]
        panel = pd.DataFrame({
            "close": close_t, "close_120d_ago": base120, "close_60d_ago": base60,
            "vol_60d": vol60,
        }).join(fundamentals, how="inner").dropna(subset=["close"])
        if len(panel) < 50:
            continue
        factors = compute_factors(panel)
        score = composite_score(factors)
        fr = ret20.loc[t]
        for name in FACTOR_NAMES:
            if name in factors.columns:
                row[name] = rank_ic(factors[name], fr)
        row["composite"] = rank_ic(score, fr)
        row["date"] = str(t)[:10]
        ic_rows.append(row)
    if not ic_rows:
        return {"ok": False, "reason": "横截面样本不足（每期需 ≥50 只有效标的）"}
    df = pd.DataFrame(ic_rows).set_index("date")
    table = {}
    for col in df.columns:
        table[col] = {"ic_mean": round(float(df[col].mean()), 4),
                      "t_stat": round(ic_t_stat(df[col]), 2),
                      "significant": bool(abs(ic_t_stat(df[col])) > 2)}
    return {"ok": bool(len(df)), "periods": len(df), "ic_table": table,
            "ic_series": {c: [None if np.isnan(x) else round(x, 4) for x in df[c]]
                          for c in df.columns}}
