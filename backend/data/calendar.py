"""交易日历（AKShare 新浪源）本地缓存，跨年自动补抓；DEMO_CLOCK 可注入假时钟单测。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
CALENDAR_CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "trade_dates.json"


def now_cst(fake_clock=None) -> datetime:
    """统一 Asia/Shanghai 时区（东财数据天然对齐）。"""
    if fake_clock is not None:
        return fake_clock
    return datetime.now(CST)


def _default_calendar(years: int = 3) -> set[date]:
    """兜底日历：周一至周五视为交易日（缺网时的保守口径）。"""
    today = date.today()
    days: set[date] = set()
    d = date(today.year - years + 1, 1, 1)
    while d <= date(today.year + 1, 12, 31):
        if d.weekday() < 5:
            days.add(d)
        d += timedelta(days=1)
    return days


class TradeCalendar:
    """交易日历：真实节假日数据缓存到本地 JSON，跨年自动补抓。"""

    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or CALENDAR_CACHE
        self._lock = threading.Lock()
        self._trade_dates: set[date] = set()
        self._load_or_init()

    def _load_or_init(self) -> None:
        try:
            if self.cache_path.exists():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self._trade_dates = {date.fromisoformat(x) for x in data}
                if max(self._trade_dates).year < date.today().year:
                    self._stale_years = True
                return
        except (json.JSONDecodeError, ValueError):
            pass
        self._trade_dates = _default_calendar()
        self._stale_years = True

    def is_trade_date(self, d: date | datetime | None = None) -> bool:
        if isinstance(d, datetime):
            d = d.date()
        d = d or now_cst().date()
        if not self._trade_dates:
            return d.weekday() < 5
        return d in self._trade_dates

    def is_trading_hours(self, dt: datetime | None = None) -> bool:
        """A 股连续竞价 09:30-11:30 / 13:00-15:00（含集合竞价 09:15-09:25 视为盘中前段）。"""
        dt = dt or now_cst()
        if not self.is_trade_date(dt):
            return False
        t = dt.time()
        from datetime import time as dtime

        return (dtime(9, 15) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))

    async def refresh(self, ak=None) -> bool:
        """补抓交易日历（ak.tool_trade_date_hist_sina）。"""
        if ak is None:
            try:
                import akshare as ak  # noqa: PLC0415
            except ImportError:
                return False
        def _fetch():
            df = ak.tool_trade_date_hist_sina()
            return {date.fromisoformat(str(x)) for x in df["trade_date"]}
        try:
            async with asyncio.timeout(30):
                days = await asyncio.get_running_loop().run_in_executor(None, _fetch)
            with self._lock:
                self._trade_dates = days
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(
                    json.dumps(sorted(x.isoformat() for x in days), ensure_ascii=False),
                    encoding="utf-8",
                )
            return True
        except Exception:
            logger.warning("交易日历补抓失败，沿用本地缓存/兜底日历", exc_info=True)
            return False
