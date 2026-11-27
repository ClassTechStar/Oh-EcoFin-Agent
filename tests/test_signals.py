"""AlphaModel 契约与确定性信号合成测试。

覆盖验收点：
- 弃权必须零观点且有原因（校验器强制）；
- 权限语义：long_only 的看空只能减少持有，不能制造空头证据；
- dollar_neutral 缺边必须显式 flat，不静默半仓；
- 内置模型数据不足一律 abstain；
- 合成纯函数可复现。
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from pydantic import ValidationError

from backend.quant.fixtures import synthetic_ohlcv
from backend.signals import (
    AlphaModel,
    AlphaSignal,
    BlendResult,
    EvidenceRef,
    LowVolatilityAlpha,
    MomentumAlpha,
    ShortReversalAlpha,
    SignalRegistry,
    blend_signals,
    default_models,
    registry,
)

# ---------- 数据结构 ----------

def test_signal_value_bounds():
    with pytest.raises(ValidationError):
        AlphaSignal(model_name="m", symbol="600519", as_of="2026-11-27", value=1.5)


def test_abstain_must_be_zero_and_reasoned():
    with pytest.raises(ValidationError, match="value 必须为 0"):
        AlphaSignal(model_name="m", symbol="600519", as_of="2026-11-27",
                    value=0.5, abstained=True, reason="数据不足")
    with pytest.raises(ValidationError, match="必须给出 reason"):
        AlphaSignal(model_name="m", symbol="600519", as_of="2026-11-27",
                    value=0.0, abstained=True)


def test_evidence_required_fields():
    ev = EvidenceRef(n=1, tool="get_kline", ts="2026-11-27", excerpt="close=100")
    assert ev.n == 1 and ev.tool == "get_kline"


# ---------- 模型契约 ----------

def test_make_signal_clips_and_carries_metadata():
    class Dummy(AlphaModel):
        name = "dummy"

        async def predict(self, symbol, as_of, ctx):
            return self.make_signal(symbol=symbol, as_of=as_of, value=2.5)

    sig = Dummy().make_signal(symbol="600519", as_of="2026-11-27", value=2.5)
    assert sig.value == 1.0
    assert sig.model_name == "dummy"
    assert sig.approach == "long_only"


def test_registry_rejects_duplicate():
    reg = SignalRegistry()

    class A(AlphaModel):
        name = "a"

        async def predict(self, symbol, as_of, ctx):
            return self.abstain(symbol, as_of, "n/a")

    reg.register(A())
    with pytest.raises(ValueError, match="重复注册"):
        reg.register(A())
    with pytest.raises(KeyError):
        reg.get("nope")


class FakeCtx:
    """离线数据上下文：直接给一段合成日线。"""

    def __init__(self, bars: pd.DataFrame):
        self.bars = bars

    async def daily_bars(self, symbol, days=120, end_date=None):
        df = self.bars
        if end_date:
            df = df[df["date"].astype(str).str.slice(0, 10) <= end_date]
        return df.tail(days + 1).reset_index(drop=True)

    async def quote(self, symbol):
        return {"symbol": symbol, "price": 100.0}


def test_builtin_models_produce_signal_with_evidence():
    bars = synthetic_ohlcv(300, seed=11)
    ctx = FakeCtx(bars)
    as_of = str(bars["date"].iloc[-1])[:10]
    for model in (MomentumAlpha(), ShortReversalAlpha(), LowVolatilityAlpha()):
        sig = asyncio.run(model.predict("600519", as_of, ctx))
        assert sig.abstained is False, f"{model.name} 不应弃权"
        assert -1.0 <= sig.value <= 1.0
        assert sig.evidence and sig.evidence[0].tool == "daily_bars"
        assert sig.horizon_days == model.horizon_days


def test_builtin_models_abstain_on_short_history():
    bars = synthetic_ohlcv(30, seed=12)
    ctx = FakeCtx(bars)
    as_of = str(bars["date"].iloc[-1])[:10]
    for model in (MomentumAlpha(), LowVolatilityAlpha()):
        sig = asyncio.run(model.predict("600519", as_of, ctx))
        assert sig.abstained is True
        assert sig.value == 0.0 and sig.reason


def test_builtin_models_abstain_on_provider_error():
    class BrokenCtx:
        async def daily_bars(self, *a, **kw):
            raise RuntimeError("provider down")

        async def quote(self, symbol):
            return {}

    for model in (MomentumAlpha(), ShortReversalAlpha(), LowVolatilityAlpha()):
        sig = asyncio.run(model.predict("600519", "2026-11-27", BrokenCtx()))
        assert sig.abstained is True


def test_default_models_idempotent_registration():
    a = default_models()
    b = default_models()
    assert [m.name for m in a] == [m.name for m in b]
    assert {m.name for m in b}.issubset(set(registry.names()))


# ---------- 合成 ----------

def _sig(name: str, symbol: str, value: float, approach: str = "long_only",
         abstained: bool = False) -> AlphaSignal:
    # 契约要求：弃权必须零观点，这里由构造器统一归零（模型侧由 abstain() 保证）
    return AlphaSignal(
        model_name=name, symbol=symbol, as_of="2026-11-27",
        value=0.0 if abstained else value,
        approach=approach, abstained=abstained,
        reason="data" if abstained else "",
    )


def test_blend_weighted_average():
    out = blend_signals(
        [_sig("a", "600519", 1.0), _sig("b", "600519", 0.0)],
        {"a": 1.0, "b": 1.0}, gross_target=0.8,
    )
    assert isinstance(out, BlendResult)
    assert out.convictions["600519"] == pytest.approx(0.5)
    assert out.weights["600519"] == pytest.approx(0.8)


def test_blend_abstained_excluded():
    out = blend_signals(
        [_sig("a", "600519", 1.0), _sig("b", "600519", -1.0, abstained=True)],
        {"a": 1.0, "b": 1.0}, gross_target=1.0,
    )
    # 弃权不参与平均：若参与则 conviction=0
    assert out.convictions["600519"] == pytest.approx(1.0)


def test_long_only_bearish_does_not_create_short_evidence():
    """long_only 模型的看空只减少持有，不制造空头证据 → 多空对冲后 flat。"""
    out = blend_signals(
        [_sig("a", "600519", 1.0), _sig("b", "600519", -1.0)],
        {"a": 1.0, "b": 1.0}, gross_target=1.0, mode="long_short",
    )
    assert out.convictions["600519"] == pytest.approx(0.0)
    assert out.eligible_scores["600519"] == pytest.approx(0.0)
    assert out.flat_reason == "no_eligible_positions"


def test_long_short_model_negative_creates_short_side():
    out = blend_signals(
        [_sig("a", "600519", -1.0, approach="long_short")],
        {"a": 1.0}, gross_target=1.0, mode="long_short",
    )
    assert out.eligible_scores["600519"] == pytest.approx(-1.0)


def test_dollar_neutral_splits_both_sides():
    out = blend_signals(
        [_sig("a", "600519", 1.0, approach="long_short"),
         _sig("b", "000001", -1.0, approach="long_short")],
        {"a": 1.0, "b": 1.0}, gross_target=1.0, mode="dollar_neutral",
    )
    assert out.weights["600519"] == pytest.approx(0.5)
    assert out.weights["000001"] == pytest.approx(-0.5)
    assert out.flat_reason is None


def test_dollar_neutral_missing_short_side_flat():
    out = blend_signals(
        [_sig("a", "600519", 1.0, approach="long_short")],
        {"a": 1.0}, gross_target=1.0, mode="dollar_neutral",
    )
    assert out.flat_reason == "missing_short_side"
    assert out.weights["600519"] == 0.0


def test_blend_invalid_inputs_raise():
    with pytest.raises(ValueError, match="未知组合模式"):
        blend_signals([_sig("a", "600519", 0.5)], {"a": 1.0}, 1.0, mode="weird")
    with pytest.raises(ValueError, match="gross_target"):
        blend_signals([_sig("a", "600519", 0.5)], {"a": 1.0}, 0.0)
    with pytest.raises(ValueError, match="权重"):
        blend_signals([_sig("a", "600519", 0.5)], {"a": 0.0}, 1.0)
    with pytest.raises(ValueError, match="未配置合成权重"):
        blend_signals([_sig("a", "600519", 0.5)], {"b": 1.0}, 1.0)


def test_blend_is_pure_and_deterministic():
    sigs = [_sig("a", "600519", 0.7), _sig("b", "600519", 0.3)]
    kwargs = dict(model_weights={"a": 2.0, "b": 1.0}, gross_target=0.9)
    r1 = blend_signals(sigs, **kwargs)
    r2 = blend_signals(sigs, **kwargs)
    assert r1.model_dump() == r2.model_dump()
