"""确定性信号合成（纯函数，零 LLM、零随机）。

移植自 ai-hedge-fund `hedge_fund/portfolio/construction.py:29-105`，语义逐条对齐：

1. 弃权票从加权平均中剔除；中性票仍参与投票（中性是有信息量的观点）。
2. 空头证据只由具备做空权限（long_short）的模型贡献；
   long_only 模型的看空只能"减少多头持有"，不能"制造空头证据"。
3. `dollar_neutral` 多空各占 gross_target/2；任一侧缺失 → 返回 flat_reason，不静默半仓。
4. 任何非法输入（权重非有限、信号越界、未知模型）立即 ValueError，绝不静默修正。
"""

from __future__ import annotations

import math
from typing import Literal, Mapping

from pydantic import BaseModel, Field

from backend.signals.base import AlphaSignal, InvestmentApproach

PortfolioMode = Literal["long_only", "long_short", "dollar_neutral"]
FlatReason = Literal["no_eligible_positions", "missing_long_side", "missing_short_side"]

WEIGHT_TOLERANCE = 1e-9


class BlendResult(BaseModel):
    """合成结果：原始观点、可配权打分、目标权重。"""

    convictions: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    eligible_scores: dict[str, float] = Field(default_factory=dict)
    flat_reason: FlatReason | None = None


def blend_signals(
    signals: list[AlphaSignal],
    model_weights: Mapping[str, float],
    gross_target: float,
    *,
    mode: PortfolioMode = "long_only",
    investment_approaches: Mapping[str, InvestmentApproach] | None = None,
) -> BlendResult:
    """把多模型观点合成为目标权重。

    Args:
        signals: 待合成信号（同一 as_of 口径由调用方保证）
        model_weights: 模型名 → 权重（必须有限且 > 0）
        gross_target: 目标总仓位（如 0.8 表示 80%）
        mode: 组合模式
        investment_approaches: 模型名 → 权限；缺省时用信号自带的 approach
    """
    if mode not in ("long_only", "long_short", "dollar_neutral"):
        raise ValueError(f"未知组合模式: {mode!r}")
    if not math.isfinite(gross_target) or gross_target <= 0:
        raise ValueError("gross_target 必须为有限正数")
    for name, weight in model_weights.items():
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"模型 {name!r} 的权重必须为有限正数")

    weighted_sum: dict[str, float] = {}
    short_sum: dict[str, float] = {}
    weight_total: dict[str, float] = {}

    for sig in signals:
        if sig.model_name not in model_weights:
            raise ValueError(f"{sig.symbol}: 模型 {sig.model_name!r} 未配置合成权重")
        if not math.isfinite(sig.value) or not -1 <= sig.value <= 1:
            raise ValueError(f"{sig.symbol}: 模型 {sig.model_name!r} 信号越界 [{sig.value}]")
        if sig.abstained:
            continue

        approach = (investment_approaches or {}).get(sig.model_name, sig.approach)
        if approach not in ("long_only", "long_short"):
            raise ValueError(f"模型 {sig.model_name!r} 的 investment_approach 非法: {approach!r}")

        weight = model_weights[sig.model_name]
        # 空头证据：仅 long_short 可用负值；long_only 的负值不入空头
        short_value = sig.value if approach == "long_short" else max(sig.value, 0.0)

        weighted_sum[sig.symbol] = weighted_sum.get(sig.symbol, 0.0) + weight * sig.value
        short_sum[sig.symbol] = short_sum.get(sig.symbol, 0.0) + weight * short_value
        weight_total[sig.symbol] = weight_total.get(sig.symbol, 0.0) + weight

    tickers = sorted({s.symbol for s in signals})
    convictions = {
        t: (weighted_sum[t] / weight_total[t]) if weight_total.get(t) else 0.0
        for t in tickers
    }
    short_assessments = {
        t: (short_sum[t] / weight_total[t]) if weight_total.get(t) else 0.0
        for t in tickers
    }
    scores = {
        t: max(convictions[t], 0.0)
        + (min(short_assessments[t], 0.0) if mode != "long_only" else 0.0)
        for t in tickers
    }

    weights: dict[str, float] = dict.fromkeys(tickers, 0.0)
    flat_reason: FlatReason | None = None

    if mode == "dollar_neutral":
        longs = sum(max(s, 0.0) for s in scores.values())
        shorts = sum(-min(s, 0.0) for s in scores.values())
        if longs < WEIGHT_TOLERANCE and shorts < WEIGHT_TOLERANCE:
            flat_reason = "no_eligible_positions"
        elif longs < WEIGHT_TOLERANCE:
            flat_reason = "missing_long_side"
        elif shorts < WEIGHT_TOLERANCE:
            flat_reason = "missing_short_side"
        else:
            weights = {
                t: round(s / (longs if s > 0 else shorts) * (gross_target / 2), 6)
                for t, s in scores.items()
            }
    else:
        gross = sum(abs(s) for s in scores.values())
        if gross < WEIGHT_TOLERANCE:
            flat_reason = "no_eligible_positions"
        else:
            weights = {t: round(s / gross * gross_target, 6) for t, s in scores.items()}

    return BlendResult(
        convictions={k: round(v, 6) for k, v in convictions.items()},
        weights=weights,
        eligible_scores={k: round(v, 6) for k, v in scores.items()},
        flat_reason=flat_reason,
    )
