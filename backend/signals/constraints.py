"""约束层（移植 Vibe-Trading `agent/backtest/constraints.py` 的后置算子语义）。

契约（逐条可测）：
1. **保持符号**：约束只会收缩/抬升已有权重，绝不把 + 变 − 或 − 变 +；
2. **不新开/不平掉仓位**：0 永远保持 0，非 0 永远非 0（cap=0 属非法配置，直接 ValueError）;
3. **按配置顺序执行**，最多 `max_rounds` 轮直到收敛；
4. 任何非有限值/负 cap/未知分组 → ValueError，绝不静默修正。

权重形态采用 `pd.DataFrame`（index=日期，columns=标的），与方案 5.4.4 对齐；
单期 `dict` 用 `apply_to_weights` 便捷包装。
"""

from __future__ import annotations

import logging
import math
from typing import Mapping, Protocol

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROUNDS = 50
TOLERANCE = 1e-12


class WeightConstraint(Protocol):
    """权重约束算子：输入/输出同形 DataFrame。"""

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame: ...


def _validate(weights: pd.DataFrame) -> None:
    if not isinstance(weights, pd.DataFrame):
        raise TypeError("约束层只接受 pd.DataFrame（index=日期, columns=标的）")
    if weights.isna().any().any():
        raise ValueError("权重含 NaN")
    values = weights.to_numpy(dtype=float)
    if not all(math.isfinite(v) for v in values.ravel()):
        raise ValueError("权重含非有限值")


def _scaled_group(row: pd.Series, members: list[str], cap: float) -> pd.Series:
    """组内按绝对权重等比例缩放（保持符号，不改变 0）。"""
    out = row.copy()
    gross = float(sum(abs(row[s]) for s in members))
    if gross <= cap or gross <= TOLERANCE:
        return out
    scale = cap / gross
    for symbol in members:
        out[symbol] = row[symbol] * scale
    return out


class MaxWeight:
    """单标的上限（|w| ≤ cap）。"""

    def __init__(self, cap: float):
        if not math.isfinite(cap) or cap <= 0:
            raise ValueError("MaxWeight.cap 必须为有限正数")
        self.cap = float(cap)

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame:
        _validate(weights)
        return weights.clip(lower=-self.cap, upper=self.cap)


class MinWeight:
    """单标的下限（已激活标的 |w| ≥ floor；0 保持 0）。"""

    def __init__(self, floor: float):
        if not math.isfinite(floor) or floor <= 0:
            raise ValueError("MinWeight.floor 必须为有限正数")
        self.floor = float(floor)

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame:
        _validate(weights)
        out = weights.copy()

        def lift(v: float) -> float:
            if abs(v) <= TOLERANCE:
                return 0.0
            if abs(v) < self.floor:
                return math.copysign(self.floor, v)
            return v

        return out.map(lift)


class GroupExposure:
    """分组暴露上限（如行业 ≤ 30%）：组内等比缩放。"""

    def __init__(self, groups: Mapping[str, str], caps: Mapping[str, float]):
        if not groups:
            raise ValueError("GroupExposure 需要非空分组映射")
        for group, cap in caps.items():
            if not math.isfinite(cap) or cap <= 0:
                raise ValueError(f"分组 {group!r} 的 cap 必须为有限正数")
        self.groups = dict(groups)
        self.caps = dict(caps)

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame:
        _validate(weights)
        out = weights.copy()
        members: dict[str, list[str]] = {}
        for symbol, group in self.groups.items():
            if symbol in out.columns:
                members.setdefault(group, []).append(symbol)
        for group, symbols in members.items():
            cap = self.caps.get(group)
            if cap is None:
                raise ValueError(f"分组 {group!r} 缺少 cap 配置")
            out = out.apply(lambda row, s=symbols, c=cap: _scaled_group(row, s, c), axis=1)
        return out


class TurnoverCap:
    """换手上限：本期相对上期（首期相对 0）的 Σ|Δw| ≤ max_turnover。

    超限时把「变化量」等比收缩 —— 结果始终落在上期与目标之间，符号不变。
    """

    def __init__(self, max_turnover: float):
        if not math.isfinite(max_turnover) or max_turnover <= 0:
            raise ValueError("TurnoverCap.max_turnover 必须为有限正数")
        self.max_turnover = float(max_turnover)

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame:
        _validate(weights)
        out = weights.copy()
        prev = pd.Series(0.0, index=weights.columns)
        for idx in weights.index:
            target = out.loc[idx].astype(float)
            delta = target - prev
            turnover = float(delta.abs().sum())
            if turnover > self.max_turnover:
                delta = delta * (self.max_turnover / turnover)
                out.loc[idx] = prev + delta
            prev = out.loc[idx].astype(float)
        return out


def apply_to_weights(weights: Mapping[str, float],
                     constraints: list[WeightConstraint],
                     *, max_rounds: int = DEFAULT_MAX_ROUNDS) -> dict[str, float]:
    """单期权重（blend_signals 输出）→ 约束链 → dict。"""
    if not weights:
        return {}
    frame = pd.DataFrame([dict(weights)], index=["t0"], dtype=float)
    constrained = ConstraintPipeline(constraints, max_rounds=max_rounds).apply(frame)
    return {k: round(float(v), 6) for k, v in constrained.loc["t0"].items()}


class ConstraintPipeline:
    """顺序执行约束链，最多 `max_rounds` 轮直至收敛；并强制契约。"""

    def __init__(self, constraints: list[WeightConstraint],
                 max_rounds: int = DEFAULT_MAX_ROUNDS):
        if max_rounds < 1:
            raise ValueError("max_rounds 必须 ≥ 1")
        self.constraints = list(constraints)
        self.max_rounds = max_rounds

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame:
        _validate(weights)
        before = weights.astype(float).copy()
        out = before.copy()
        for _ in range(self.max_rounds):
            prev = out.copy()
            for constraint in self.constraints:
                out = constraint.apply(out)
                # 每个算子后立即验契约：避免后置算子掩盖前序越界
                self._assert_contract(before, out)
            if float((out - prev).abs().to_numpy().max()) <= TOLERANCE:
                break
        self._assert_contract(before, out)
        return out

    @staticmethod
    def _assert_contract(before: pd.DataFrame, after: pd.DataFrame) -> None:
        if list(before.columns) != list(after.columns):
            raise ValueError("约束层改变了标的集合（禁止新开/平掉仓位）")
        b = before.to_numpy(dtype=float)
        a = after.to_numpy(dtype=float)
        for i in range(b.shape[0]):
            for j in range(b.shape[1]):
                was, now = b[i, j], a[i, j]
                if abs(was) <= TOLERANCE and abs(now) > TOLERANCE:
                    raise ValueError(f"约束层新开仓位: {before.columns[j]} 0→{now}")
                if abs(was) > TOLERANCE and abs(now) <= TOLERANCE:
                    raise ValueError(f"约束层平掉仓位: {before.columns[j]} {was}→0")
                if was * now < 0:
                    raise ValueError(f"约束层改变符号: {before.columns[j]} {was}→{now}")
