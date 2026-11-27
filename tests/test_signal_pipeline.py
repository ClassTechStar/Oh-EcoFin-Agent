"""信号流水线（M7）：点位视图 / 失败可见 / 合成+约束 / 信号与目标落库。"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from backend.db import Database
from backend.quant.fixtures import synthetic_ohlcv
from backend.signals import (
    AlphaModel,
    MarketDataContext,
    MaxWeight,
    SignalStore,
    compose,
    run_models,
)
from backend.signals.base import AlphaSignal


class FakeMarket:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.calls: list = []

    async def get_kline_raw(self, symbol: str, days: int, adjust: str) -> pd.DataFrame:
        self.calls.append((symbol, days, adjust))
        return self.df.copy()

    async def get_quote(self, symbol: str) -> dict:
        return {"symbol": symbol, "price": 100.0}


class FixedAlpha(AlphaModel):
    def __init__(self, name: str, value: float, approach: str = "long_only"):
        self.name = name
        self.value = value
        self.investment_approach = approach

    async def predict(self, symbol, as_of, ctx):
        return self.make_signal(symbol=symbol, as_of=as_of, value=self.value)


class BoomAlpha(AlphaModel):
    name = "boom"

    async def predict(self, symbol, as_of, ctx):
        raise RuntimeError("模型内部崩溃")


def test_market_context_cuts_future_rows():
    df = synthetic_ohlcv(days=40)
    market = FakeMarket(df)
    cut = str(df["date"].iloc[20])
    ctx = MarketDataContext(market, as_of=cut)
    bars = asyncio.run(ctx.daily_bars("600519"))
    assert len(bars) == 21
    assert bars["date"].iloc[-1] == cut
    # end_date 优先于构造时 as_of
    cut2 = str(df["date"].iloc[10])
    bars2 = asyncio.run(ctx.daily_bars("600519", end_date=cut2))
    assert bars2["date"].iloc[-1] == cut2
    quote = asyncio.run(ctx.quote("600519"))
    assert quote["price"] == 100.0
    assert market.calls[0][2] == "hfq"


async def test_run_models_records_failure_without_fake_signal():
    market = FakeMarket(synthetic_ohlcv(days=200))
    ctx = MarketDataContext(market, as_of="2026-11-27")
    run = await run_models([FixedAlpha("fixed", 0.5), BoomAlpha()],
                           ["600519", "000001"], "2026-11-27", ctx)
    assert len(run.signals) == 2  # fixed × 2 标的
    assert len(run.failures) == 2
    assert {f["model"] for f in run.failures} == {"boom"}
    assert all("RuntimeError" in f["error"] for f in run.failures)
    assert run.to_dict()["as_of"] == "2026-11-27"


async def test_compose_applies_constraints_after_blend():
    market = FakeMarket(synthetic_ohlcv(days=200))
    ctx = MarketDataContext(market)
    run = await run_models([FixedAlpha("fixed", 1.0)], ["600519"],
                           "2026-11-27", ctx)
    out = compose(run, {"fixed": 1.0}, 0.8)
    assert out.weights["600519"] == pytest.approx(0.8)
    assert out.constraints_applied is False

    capped = compose(run, {"fixed": 1.0}, 0.8, constraints=[MaxWeight(0.05)])
    assert capped.raw_weights["600519"] == pytest.approx(0.8)
    assert capped.weights["600519"] == pytest.approx(0.05)
    assert capped.constraints_applied is True
    assert capped.to_dict()["flat_reason"] is None


async def test_dollar_neutral_missing_side_is_flat_not_half_position():
    market = FakeMarket(synthetic_ohlcv(days=200))
    ctx = MarketDataContext(market)
    run = await run_models([FixedAlpha("long", 0.6)], ["600519"],
                           "2026-11-27", ctx)
    out = compose(run, {"long": 1.0}, 0.8, mode="dollar_neutral")
    assert out.blend.flat_reason == "missing_short_side"
    assert out.weights["600519"] == 0.0


def test_signal_store_roundtrip(tmp_path):
    db = Database(tmp_path / "signals.db")
    store = SignalStore(db)
    sig = AlphaSignal(model_name="momentum_120d", symbol="600519",
                      as_of="2026-11-27", value=0.42, confidence=0.7,
                      horizon_days=20, evidence=[])
    abstain = AlphaSignal(model_name="momentum_120d", symbol="000001",
                          as_of="2026-11-27", value=0.0, abstained=True,
                          reason="样本不足")
    assert store.save_signals([sig, abstain]) == 2

    rows = store.recent_signals(model_name="momentum_120d", limit=10)
    assert len(rows) == 2
    assert {r["abstained"] for r in rows} == {False, True}
    one = next(r for r in rows if r["symbol"] == "600519")
    assert one["value"] == pytest.approx(0.42)
    assert one["horizon_days"] == 20

    target_id = store.save_target("dual_ma", "2026-11-27",
                                  {"600519": 0.5, "000001": 0.0},
                                  [{"type": "max_weight", "cap": 0.25}], None)
    assert target_id > 0
    targets = store.recent_targets(strategy_id="dual_ma")
    assert targets[0]["weights"]["600519"] == pytest.approx(0.5)
    assert targets[0]["constraints"][0]["cap"] == 0.25
    assert targets[0]["flat_reason"] is None
    assert store.recent_targets(strategy_id="other") == []
