"""测试公共夹具：临时 DB、合成行情数据、模拟网关。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_db(tmp_path):
    from backend.db import Database

    return Database(tmp_path / "test.db")


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    from backend.config import Settings

    s = Settings(db_path=str(tmp_path / "test.db"))
    s.app.demo_mode = True
    s.cassette_dir = str(ROOT / "tests" / "cassettes_frozen")
    return s


def synthetic_ohlcv(days: int = 600, seed: int = 7, start: float = 100.0) -> pd.DataFrame:
    """合成日线 OHLCV（几何随机游走 + 趋势），含 date/open/high/low/close/volume。"""
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


@pytest.fixture()
def ohlcv() -> pd.DataFrame:
    return synthetic_ohlcv()


class FakeGateway:
    """LLMGateway 桩：剧本回放 tool_calls 序列（CI 零密钥零成本）。"""

    def __init__(self, script: list | None = None, simple_texts: dict | None = None):
        self.script = list(script or [])
        self.simple_texts = simple_texts or {}
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, tier="primary", *,
                   session_id="", role="chat", stream_cb=None, temperature=0.3):
        from backend.engine.gateway import LLMResult, ToolCall

        self.calls.append({"messages": messages, "tools": tools, "tier": tier, "role": role})
        if not self.script:
            return LLMResult(text="（剧本耗尽）", model="fake", tier=tier)
        step = self.script.pop(0)
        tool_calls = [ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a, ensure_ascii=False))
                      for i, (n, a) in enumerate(step.get("tool_calls", []))]
        return LLMResult(text=step.get("text", ""), tool_calls=tool_calls,
                         usage_in=step.get("in", 100), usage_out=step.get("out", 50),
                         cost_cents=step.get("cost", 1), model="fake", tier=tier,
                         finish_reason=step.get("finish", ""))

    async def simple(self, messages, tier="economy", *, session_id="", role="aux",
                     temperature=0.3, max_tokens=None):
        self.calls.append({"messages": messages, "tier": tier, "role": role})
        # 按最后一条用户消息前 12 字路由剧本
        key = next((k for k in self.simple_texts
                    if str(messages[-1]["content"]).startswith(k)), None)
        return self.simple_texts.get(key, "默认回复")
