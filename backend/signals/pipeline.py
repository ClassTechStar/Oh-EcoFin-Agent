"""信号流水线：并行跑模型 → 合成 → 约束层（确定性，LLM 不参与）。

失败语义：单个模型/标的失败只记录在 `failures`，不生成伪信号（不伪装中性）；
合成阶段自然忽略缺失票，报告仍能说明「谁没投票、为什么」。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from backend.signals.base import AlphaModel, AlphaSignal, DataContext
from backend.signals.constraints import (
    ConstraintPipeline,
    WeightConstraint,
    apply_to_weights,
)
from backend.signals.construction import BlendResult, PortfolioMode, blend_signals

logger = logging.getLogger(__name__)


@dataclass
class SignalRun:
    """一次多模型多标的取信号的结果（含失败清单）。"""

    as_of: str
    signals: list[AlphaSignal] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "signals": [s.model_dump() for s in self.signals],
            "failures": list(self.failures),
        }


async def run_models(models: Iterable[AlphaModel], symbols: list[str], as_of: str,
                     ctx: DataContext) -> SignalRun:
    """并行执行 model × symbol；异常落 failures，绝不中断整批。"""
    model_list = list(models)
    pairs = [(m, s) for m in model_list for s in symbols]
    results = await asyncio.gather(
        *(m.predict(symbol, as_of, ctx) for m, symbol in pairs),
        return_exceptions=True)

    run = SignalRun(as_of=as_of)
    for (model, symbol), res in zip(pairs, results, strict=True):
        if isinstance(res, Exception):
            logger.warning("AlphaModel %s 在 %s 上失败: %s", model.name, symbol, res)
            run.failures.append({"model": model.name, "symbol": symbol,
                                 "error": f"{type(res).__name__}: {res}"})
        else:
            run.signals.append(res)
    return run


@dataclass
class ComposeResult:
    weights: dict[str, float]
    raw_weights: dict[str, float]
    blend: BlendResult
    constraints_applied: bool
    failures: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "weights": self.weights,
            "raw_weights": self.raw_weights,
            "convictions": self.blend.convictions,
            "flat_reason": self.blend.flat_reason,
            "constraints_applied": self.constraints_applied,
            "failures": list(self.failures),
        }


def compose(run: SignalRun,
            model_weights: Mapping[str, float],
            gross_target: float,
            *,
            mode: PortfolioMode = "long_only",
            constraints: list[WeightConstraint] | None = None,
            max_rounds: int = 50) -> ComposeResult:
    """合成 + 约束；约束只作用于已激活标的，不新开/不平掉仓位。"""
    blended = blend_signals(run.signals, model_weights, gross_target, mode=mode)
    raw = dict(blended.weights)
    weights = raw
    if constraints and raw:
        pipeline = ConstraintPipeline(constraints, max_rounds=max_rounds)
        weights = apply_to_weights(raw, pipeline.constraints,
                                   max_rounds=pipeline.max_rounds)
    return ComposeResult(weights=weights, raw_weights=raw, blend=blended,
                         constraints_applied=bool(constraints and raw),
                         failures=list(run.failures))
