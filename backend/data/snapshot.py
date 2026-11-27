"""全市场快照缓存（数据层地基）。

设计要点：
1. 网络调用量恒定：盘中无论多少工具调用，出站始终 = 每 60s 1 次快照请求（架构上规避反爬）；
2. 原子替换无锁读：新 df 整体替换引用（Python 引用赋值原子），读侧永不加锁 → 工具 <50ms；
3. 降频语义：盘后/非交易日 30 分钟一次；交易日历感知；
4. 东财→新浪降级：连续 3 次失败自动切新浪源（列名映射层归一），恢复后切回；
5. 盘后回放：非交易时段返回最后一份快照并强制 is_stale=True——诚实降级。
DEMO_MODE：数据层全部从预抓冻结卡带读取（断网可完整演示）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backend.config import Settings
from backend.data.calendar import TradeCalendar, now_cst
from backend.data.providers import (MarketDataProvider, build_provider,
                                    spot_to_legacy)
from backend.data.quality import validate_snapshot_df
from backend.data.ratelimit import TokenBucket

logger = logging.getLogger(__name__)


class SnapshotService:
    """进程生命周期常驻的后台任务。"""

    def __init__(self, settings: Settings, calendar: TradeCalendar | None = None,
                 provider: MarketDataProvider | None = None):
        self.settings = settings
        self.calendar = calendar or TradeCalendar()
        self.bucket = TokenBucket(settings.data.rate_limit_rps, settings.data.rate_limit_burst)
        self.provider = provider or build_provider(settings)
        self._df: pd.DataFrame | None = None
        self._as_of: str = ""
        self._stale: bool = True
        self._source: str = "none"
        self._fail_count = 0
        self._backoff = 1.0
        self._task: asyncio.Task | None = None
        self._watchlist: set[str] = set()
        self._precompute_fn = None  # 由 precompute 模块注入

    # ---------- 读侧（无锁） ----------

    @property
    def as_of(self) -> str:
        return self._as_of

    def row(self, symbol: str) -> dict[str, Any] | None:
        """按 6 位代码取快照行（不加锁，<50ms 路径）。"""
        if self._df is None or self._df.empty:
            return None
        try:
            hit = self._df.loc[self._df["代码"].astype(str).str.zfill(6) == str(symbol).zfill(6)]
            if hit.empty:
                return None
            return hit.iloc[0].to_dict()
        except Exception:
            return None

    def rows(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        return {s: self.row(s) for s in symbols}

    def freshness(self) -> dict[str, Any]:
        from backend.data.quality import freshness

        return freshness(self._as_of, self._stale)

    def register_watchlist(self, symbols: list[str]) -> None:
        self._watchlist.update(symbols)

    def set_precompute(self, fn) -> None:
        self._precompute_fn = fn

    # ---------- 写侧 ----------

    def _swap(self, df: pd.DataFrame, source: str) -> None:
        missing = validate_snapshot_df(df)
        if missing:
            raise ValueError(f"快照缺关键列: {missing}")
        df = df.copy()
        df["代码"] = df["代码"].astype(str).str.zfill(6)
        self._df = df
        self._as_of = now_cst().strftime("%Y-%m-%d %H:%M:%S")
        self._stale = False
        self._source = source
        self._fail_count = 0
        self._backoff = 1.0

    def _mark_stale(self) -> None:
        self._stale = True

    # ---------- 拉取（东财 → 新浪降级） ----------

    # ---------- 拉取（Provider 抽象：东财 → 新浪降级 / 离线卡带） ----------

    async def refresh_once(self) -> bool:
        """单次刷新：统一走 Provider（DEMO_MODE 由 OfflineProvider 承载）。"""
        try:
            if not self.settings.app.demo_mode:
                await self.bucket.acquire()
            df = await asyncio.get_running_loop().run_in_executor(
                None, self.provider.spot_all)
            if df is None or df.empty:
                raise ValueError("Provider 返回空快照")
            legacy = spot_to_legacy(df)
            self._swap(legacy, self.provider.last_source)
            if self.settings.app.demo_mode:
                self._stale = True  # 冻结数据永远标记 stale（诚实降级）
            return True
        except Exception as e:
            self._fail_count += 1
            self._mark_stale()
            logger.warning("快照刷新失败(#%d): %s", self._fail_count, e)
            return False

    def _interval(self) -> int:
        cfg = self.settings.data
        if not self.calendar.is_trade_date(now_cst()):
            return cfg.snapshot_interval_idle
        if self.calendar.is_trading_hours(now_cst()):
            return cfg.snapshot_interval_intraday
        return cfg.snapshot_interval_idle

    async def _backoff_wait(self) -> None:
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, 60.0)

    async def loop(self) -> None:
        """主循环：指数退避 1s→2s→…→60s 封顶。"""
        while True:
            try:
                ok = await self.refresh_once()
                if ok and self._precompute_fn and self._watchlist:
                    try:
                        await self._precompute_fn(sorted(self._watchlist))
                    except Exception:
                        logger.exception("预计算失败")
            except Exception:
                logger.exception("快照循环异常")
                await self._backoff_wait()
                continue
            await asyncio.sleep(self._interval())

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
