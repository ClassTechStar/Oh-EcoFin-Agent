"""确定性行情 fixture（离线可复现，供测试与 CI 审计门禁共用）。

设计约束：
- 同一 seed → 逐字节一致的 DataFrame（截断检验需要严格可复现）；
- 含趋势切换段（前半上行/后半下行），使双均线策略必然产生成交；
- 不联网、无随机时变，CI 零密钥可跑。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_ohlcv(days: int = 600, seed: int = 7, start: float = 100.0) -> pd.DataFrame:
    """合成日线 OHLCV（几何随机游走 + 趋势切换）。

    返回列：date / open / high / low / close / volume（与 Provider 归一化契约一致）。
    """
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.full(days // 2, 0.0004), np.full(days - days // 2, -0.0002)])
    ret = rng.normal(drift, 0.02, days)
    close = start * np.cumprod(1 + ret)
    open_ = close * (1 + rng.normal(0, 0.004, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, days)))
    volume = rng.integers(1_000_000, 8_000_000, days).astype(float)
    dates = pd.bdate_range("2024-01-02", periods=days).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": open_, "high": high,
                         "low": low, "close": close, "volume": volume})
