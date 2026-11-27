"""约束层（M7）：保符号 / 不新开不平掉 / 顺序执行 / 非法配置快速失败。"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.signals.constraints import (
    ConstraintPipeline,
    GroupExposure,
    MaxWeight,
    MinWeight,
    TurnoverCap,
    apply_to_weights,
)


def frame(rows: dict[str, dict[str, float]]) -> pd.DataFrame:
    return pd.DataFrame.from_dict(rows, orient="index", dtype=float)


def test_max_weight_caps_and_keeps_sign_and_zero():
    df = frame({"t1": {"A": 0.6, "B": -0.4, "C": 0.0},
                "t2": {"A": 0.2, "B": -0.1, "C": 0.0}})
    out = MaxWeight(0.3).apply(df)
    assert out.loc["t1", "A"] == pytest.approx(0.3)
    assert out.loc["t1", "B"] == pytest.approx(-0.3)
    assert out.loc["t1", "C"] == 0.0
    assert (out["C"] == 0).all()


def test_min_weight_lifts_only_active_positions():
    df = frame({"t1": {"A": 0.05, "B": -0.02, "C": 0.0}})
    out = MinWeight(0.1).apply(df)
    assert out.loc["t1", "A"] == pytest.approx(0.1)
    assert out.loc["t1", "B"] == pytest.approx(-0.1)
    assert out.loc["t1", "C"] == 0.0  # 不新开仓位


def test_group_exposure_scales_group_members():
    df = frame({"t1": {"A": 0.4, "B": 0.2, "C": 0.4}})
    out = GroupExposure({"A": "tech", "B": "tech", "C": "bank"},
                        {"tech": 0.3, "bank": 0.5}).apply(df)
    assert out.loc["t1", "A"] == pytest.approx(0.2)
    assert out.loc["t1", "B"] == pytest.approx(0.1)
    assert out.loc["t1", "C"] == pytest.approx(0.4)
    assert abs(out.loc["t1", ["A", "B"]]).sum() == pytest.approx(0.3)


def test_turnover_cap_limits_delta_and_keeps_sign():
    df = frame({"t1": {"A": 0.5, "B": -0.5},
                "t2": {"A": 0.8, "B": -0.2}})
    out = TurnoverCap(0.5).apply(df)
    first = out.loc["t1"]
    assert first.abs().sum() == pytest.approx(0.5)
    delta = (out.loc["t2"] - first).abs().sum()
    assert delta <= 0.5 + 1e-9
    assert (out.loc["t2", "A"] > 0) and (out.loc["t2", "B"] < 0)


class _Bad:
    def __init__(self, kind: str):
        self.kind = kind

    def apply(self, weights: pd.DataFrame) -> pd.DataFrame:
        out = weights.copy()
        if self.kind == "close":
            out.iloc[0, 0] = 0.0
        elif self.kind == "open":
            if "C" in out.columns:
                out.iloc[0, out.columns.get_loc("C")] = 0.2
        else:
            out.iloc[0, 0] = -out.iloc[0, 0]
        return out


@pytest.mark.parametrize(("kind", "match"), [
    ("close", "平掉仓位"), ("open", "新开仓位"), ("flip", "改变符号"),
])
def test_pipeline_rejects_contract_violation(kind, match):
    df = frame({"t1": {"A": 0.4, "C": 0.0}})
    with pytest.raises(ValueError, match=match):
        ConstraintPipeline([_Bad(kind)]).apply(df)


def test_pipeline_converges_and_composes_in_order():
    df = frame({"t1": {"A": 0.6, "B": 0.4}})
    out = ConstraintPipeline([MaxWeight(0.5), GroupExposure(
        {"A": "g", "B": "g"}, {"g": 0.6})]).apply(df)
    assert out.loc["t1", "A"] == pytest.approx(0.5 / 0.9 * 0.6)
    assert out.loc["t1", "B"] == pytest.approx(0.4 / 0.9 * 0.6)
    assert out.loc["t1", ["A", "B"]].abs().sum() == pytest.approx(0.6)


def test_apply_to_weights_single_period():
    assert apply_to_weights({}, [MaxWeight(0.3)]) == {}
    out = apply_to_weights({"A": 0.8, "B": -0.6, "C": 0.0}, [MaxWeight(0.5)])
    assert out == {"A": 0.5, "B": -0.5, "C": 0.0}


@pytest.mark.parametrize("factory", [
    lambda: MaxWeight(0), lambda: MinWeight(-0.1),
    lambda: GroupExposure({}, {}), lambda: TurnoverCap(0),
])
def test_invalid_config_fails_fast(factory):
    with pytest.raises(ValueError):
        factory()


def test_nan_and_inf_rejected():
    with pytest.raises(ValueError, match="NaN"):
        MaxWeight(0.3).apply(pd.DataFrame([{"A": float("nan")}]))
    with pytest.raises(ValueError, match="非有限"):
        MaxWeight(0.3).apply(pd.DataFrame([{"A": float("inf")}]))
