"""令牌桶限速（全局出站总闸，AKShare 专用）。

所有 AKShare 调用（快照/K线/财务/交易日历）必须先过桶；
搜索三源走各自独立的桶。
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """异步令牌桶：rate tokens/s，容量 capacity 允许短突发。"""

    def __init__(self, rate: float = 1.0, capacity: int = 3):
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    async def acquire(self, timeout: float | None = 30.0) -> None:
        """取 1 个令牌；超时抛 TimeoutError。"""
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                need = (1 - self._tokens) / self.rate
            if deadline is not None and time.monotonic() + need > deadline:
                raise TimeoutError("限速等待超时（令牌桶）")
            await asyncio.sleep(min(need, 0.5))
