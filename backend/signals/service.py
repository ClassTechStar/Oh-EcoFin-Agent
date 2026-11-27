"""信号预览服务：REST 看板与 Agent 工具共用同一口径（避免双份实现漂移）。"""

from __future__ import annotations

import asyncio
import logging

from backend.signals import (
    MarketDataContext,
    MaxWeight,
    SignalStore,
    compose,
    default_models,
    run_models,
)

logger = logging.getLogger(__name__)

#: 单标的预览的默认约束（与看板展示一致）
DEFAULT_MAX_WEIGHT = 0.25


class SignalDataError(ValueError):
    """信号所需的行情不可用/样本不足（调用方转 400/503 或工具错误）。"""


async def preview_signals(market, store: SignalStore | None, symbol: str, *,
                          mode: str = "long_only", gross: float = 0.8,
                          days: int = 200) -> dict:
    """取数 → 多模型并行 → 确定性合成 → 约束层 → 落库。"""
    if mode not in ("long_only", "long_short", "dollar_neutral"):
        raise SignalDataError(f"未知组合模式: {mode}")
    if not (0 < float(gross) <= 1):
        raise SignalDataError("gross_target 必须在 (0, 1] 内")

    try:
        df = await market.get_kline_raw(symbol, days, "hfq")
    except Exception as e:
        raise SignalDataError(f"K线获取失败: {e}") from e
    if df is None or len(df) < 60:
        raise SignalDataError(
            f"{symbol} K线不足（{0 if df is None else len(df)} 根）")

    as_of = str(df["date"].iloc[-1])[:10]
    data_ctx = MarketDataContext(market, as_of=as_of)
    models = default_models()
    run = await run_models(models, [symbol], as_of, data_ctx)
    result = compose(run, {m.name: 1.0 for m in models}, gross,
                     mode=mode, constraints=[MaxWeight(DEFAULT_MAX_WEIGHT)])

    saved = 0
    if store is not None:
        try:
            saved = await asyncio.to_thread(store.save_signals, run.signals)
            await asyncio.to_thread(
                store.save_target, f"signals:{mode}", as_of, result.weights,
                [{"type": "max_weight", "cap": DEFAULT_MAX_WEIGHT}],
                result.blend.flat_reason)
        except Exception:
            logger.exception("信号落库失败（不影响预览结果）")

    return {
        "ok": True,
        "symbol": symbol,
        "as_of": as_of,
        "mode": mode,
        "gross_target": gross,
        "models": [{"name": m.name, "approach": m.investment_approach,
                    "horizon_days": m.horizon_days} for m in models],
        "signals": [s.model_dump() for s in run.signals],
        "weights": result.weights,
        "raw_weights": result.raw_weights,
        "convictions": result.blend.convictions,
        "flat_reason": result.blend.flat_reason,
        "failures": result.failures,
        "constraints": [{"type": "max_weight", "cap": DEFAULT_MAX_WEIGHT}],
        "saved_signals": saved,
    }
