"""内置量化 Alpha 模型（纯规则，零 LLM，可离线复现）。

三个模型刻意覆盖不同风格与周期，用于验证合成层的权限语义：
| 模型 | 风格 | 周期 | 方向 |
|---|---|---|---|
| MomentumAlpha | 动量 | 20 交易日 | 追涨 |
| ShortReversalAlpha | 短期反转 | 5 交易日 | 抄跌 |
| LowVolatilityAlpha | 低波动 | 20 交易日 | 避险 |

数据不足一律 abstain，不猜测、不外推。
"""

from __future__ import annotations

import math

import pandas as pd

from backend.signals.base import AlphaModel, AlphaSignal, DataContext, EvidenceRef, registry


async def _load_bars(ctx: DataContext, symbol: str, as_of: str, days: int) -> pd.DataFrame | None:
    """取点时日线并做最小校验；任何异常都降级为 None（由调用方 abstain）。"""
    try:
        bars = await ctx.daily_bars(symbol, days=days, end_date=as_of)
    except Exception:
        return None
    if bars is None or len(bars) == 0 or "close" not in getattr(bars, "columns", []):
        return None
    out = bars.copy()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna(subset=["close"])


def _evidence(last_date: str, symbol: str, days: int, excerpt: str) -> list[EvidenceRef]:
    return [EvidenceRef(
        n=1, tool="daily_bars", args=f"symbol={symbol},days={days}",
        ts=last_date[:10], excerpt=excerpt,
    )]


class MomentumAlpha(AlphaModel):
    """120 日动量：涨幅越大越看多（tanh 压缩到 [-1,1]）。"""

    name = "momentum_120d"
    investment_approach = "long_only"
    horizon_days = 20
    LOOKBACK = 120

    async def predict(self, symbol: str, as_of: str, ctx: DataContext) -> AlphaSignal:
        bars = await _load_bars(ctx, symbol, as_of, self.LOOKBACK + 20)
        if bars is None or len(bars) < int(self.LOOKBACK * 0.8):
            return self.abstain(symbol, as_of, f"K线不足 {self.LOOKBACK} 根")
        close = bars["close"]
        ret = float(close.iloc[-1] / close.iloc[-self.LOOKBACK] - 1)
        if not math.isfinite(ret):
            return self.abstain(symbol, as_of, "动量计算非有限值")
        last_date = str(bars["date"].iloc[-1]) if "date" in bars.columns else as_of
        value = math.tanh(ret * 8.0)
        return self.make_signal(
            symbol=symbol, as_of=as_of, value=value, confidence=0.65,
            evidence=_evidence(last_date, symbol, self.LOOKBACK,
                               f"{self.LOOKBACK}d 收益 {ret:+.2%}，close={close.iloc[-1]:.2f}"),
        )


class ShortReversalAlpha(AlphaModel):
    """5 日反转：短期跌幅越大越看多（负向惯性修正）。"""

    name = "reversal_5d"
    investment_approach = "long_only"
    horizon_days = 5
    LOOKBACK = 5

    async def predict(self, symbol: str, as_of: str, ctx: DataContext) -> AlphaSignal:
        bars = await _load_bars(ctx, symbol, as_of, self.LOOKBACK + 15)
        if bars is None or len(bars) <= self.LOOKBACK:
            return self.abstain(symbol, as_of, f"K线不足 {self.LOOKBACK + 1} 根")
        close = bars["close"]
        ret = float(close.iloc[-1] / close.iloc[-1 - self.LOOKBACK] - 1)
        if not math.isfinite(ret):
            return self.abstain(symbol, as_of, "反转计算非有限值")
        last_date = str(bars["date"].iloc[-1]) if "date" in bars.columns else as_of
        value = math.tanh(-ret * 15.0)
        return self.make_signal(
            symbol=symbol, as_of=as_of, value=value, confidence=0.55,
            evidence=_evidence(last_date, symbol, self.LOOKBACK + 1,
                               f"{self.LOOKBACK}d 收益 {ret:+.2%}（反向计分）"),
        )


class LowVolatilityAlpha(AlphaModel):
    """60 日波动越低越看多（低波动异象）。"""

    name = "lowvol_60d"
    investment_approach = "long_only"
    horizon_days = 20
    LOOKBACK = 60
    #: 年化波动锚点：30% 视为中性
    ANCHOR_VOL = 0.30

    async def predict(self, symbol: str, as_of: str, ctx: DataContext) -> AlphaSignal:
        bars = await _load_bars(ctx, symbol, as_of, self.LOOKBACK + 20)
        if bars is None or len(bars) < int(self.LOOKBACK * 0.8):
            return self.abstain(symbol, as_of, f"K线不足 {self.LOOKBACK} 根")
        close = bars["close"].tail(self.LOOKBACK + 1)
        rets = close.pct_change().dropna()
        if len(rets) < 20 or float(rets.std()) == 0.0:
            return self.abstain(symbol, as_of, "波动率样本不足")
        ann_vol = float(rets.std() * math.sqrt(244))
        if not math.isfinite(ann_vol):
            return self.abstain(symbol, as_of, "波动率非有限值")
        last_date = str(bars["date"].iloc[-1]) if "date" in bars.columns else as_of
        value = math.tanh((self.ANCHOR_VOL - ann_vol) * 8.0)
        return self.make_signal(
            symbol=symbol, as_of=as_of, value=value, confidence=0.6,
            evidence=_evidence(last_date, symbol, self.LOOKBACK,
                               f"{self.LOOKBACK}d 年化波动 {ann_vol:.1%}（锚点 {self.ANCHOR_VOL:.0%}）"),
        )


def default_models() -> list[AlphaModel]:
    """返回内置模型并幂等注册到默认注册表。"""
    models: list[AlphaModel] = [MomentumAlpha(), ShortReversalAlpha(), LowVolatilityAlpha()]
    existing = set(registry.names())
    for m in models:
        if m.name not in existing:
            registry.register(m)
    return models
