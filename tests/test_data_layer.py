"""交易日历（DEMO_CLOCK 假时钟单测，不依赖真实日历）/ 令牌桶 / 快照服务。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from backend.data.calendar import CST, TradeCalendar, now_cst
from backend.data.precompute import compute_indicators
from backend.data.ratelimit import TokenBucket
from backend.data.snapshot import SnapshotService


def make_calendar(tmp_path, dates):
    p = tmp_path / "trade_dates.json"
    import json

    p.write_text(json.dumps(sorted(d.isoformat() for d in dates)), encoding="utf-8")
    return TradeCalendar(cache_path=p)


def test_weekend_not_trade_date(tmp_path):
    from datetime import date

    # 2026-11-28 周六
    cal = make_calendar(tmp_path, [date(2026, 11, 27)])
    assert cal.is_trade_date(datetime(2026, 11, 27, 10, 0, tzinfo=CST)) is True
    assert cal.is_trade_date(datetime(2026, 11, 28, 10, 0, tzinfo=CST)) is False


def test_trading_hours_windows(tmp_path):
    from datetime import date

    cal = make_calendar(tmp_path, [date(2026, 11, 27)])
    d = datetime(2026, 11, 27, tzinfo=CST)
    assert cal.is_trading_hours(d.replace(hour=10, minute=0)) is True   # 早盘
    assert cal.is_trading_hours(d.replace(hour=12, minute=0)) is False  # 午休
    assert cal.is_trading_hours(d.replace(hour=14, minute=0)) is True   # 午盘
    assert cal.is_trading_hours(d.replace(hour=15, minute=30)) is False  # 收盘后
    assert cal.is_trading_hours(d.replace(hour=8, minute=0)) is False   # 开盘前
    # 周末全天 false
    assert cal.is_trading_hours(d.replace(hour=10, minute=0)) is True
    sat = d.replace(hour=10) + timedelta(days=1)
    assert cal.is_trading_hours(sat) is False


def test_clock_injection():
    """DEMO_CLOCK 可注入假时钟单测（不依赖真实日历）。"""
    fake = datetime(2026, 1, 1, 10, 0, tzinfo=CST)
    assert now_cst(fake) is fake


# ---------- 令牌桶 ----------

async def test_token_bucket_burst_and_rate():
    bucket = TokenBucket(rate=10.0, capacity=3)
    t0 = time.monotonic()
    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()  # 容量 3 允许短突发
    burst_elapsed = time.monotonic() - t0
    assert burst_elapsed < 0.5
    await bucket.acquire()  # 第 4 个需等待补充
    assert time.monotonic() - t0 >= 0.05


async def test_token_bucket_timeout():
    bucket = TokenBucket(rate=0.5, capacity=1)
    await bucket.acquire()
    with pytest.raises(TimeoutError):
        await bucket.acquire(timeout=0.3)


# ---------- 快照服务 ----------

def make_snapshot_df():
    import pandas as pd

    return pd.DataFrame({
        "代码": ["600519", "000001", "300750"],
        "名称": ["贵州茅台", "平安银行", "宁德时代"],
        "最新价": [1500.0, 12.5, 210.0],
        "涨跌幅": [1.2, -0.5, 9.99],
        "成交量": [12345, 0, 99999],  # 000001 停牌
        "换手率": [0.5, 0.3, 1.1],
    })


def make_settings(tmp_path, demo=False):
    s = type("S", (), {})()
    s.data = type("D", (), {
        "rate_limit_rps": 100.0, "rate_limit_burst": 3,
        "snapshot_interval_intraday": 60, "snapshot_interval_idle": 1800,
        "kline_cache_ttl": 3600, "news_cache_ttl": 1800, "notice_cache_ttl": 600,
    })()
    s.app = type("A", (), {"demo_mode": demo})()
    s.cassette_dir = str(tmp_path / "cassette")
    return s


def test_snapshot_atomic_swap_and_read(tmp_path):
    svc = SnapshotService(make_settings(tmp_path))
    svc._swap(make_snapshot_df(), "eastmoney")
    row = svc.row("600519")
    assert row["名称"] == "贵州茅台"
    assert svc.row("999999") is None
    assert svc.as_of != ""
    assert svc.freshness()["freshness"] in ("green", "yellow")


async def test_snapshot_demo_cassette(tmp_path):
    """DEMO_MODE 从冻结卡带读取且强制 is_stale=True（诚实降级）。"""
    import json

    cass_dir = tmp_path / "cassette"
    cass_dir.mkdir()
    (cass_dir / "snapshot.json").write_text(
        json.dumps(make_snapshot_df().to_dict(orient="records"),
                   ensure_ascii=False), encoding="utf-8")
    svc = SnapshotService(make_settings(tmp_path, demo=True))
    ok = await svc.refresh_once()
    assert ok is True
    assert svc.row("600519") is not None
    assert svc.freshness()["is_stale"] is True
    assert svc._source == "cassette"


async def test_snapshot_cassette_missing(tmp_path):
    svc = SnapshotService(make_settings(tmp_path, demo=True))
    assert await svc.refresh_once() is False


def test_snapshot_row_zero_volume_suspension_path(tmp_path):
    svc = SnapshotService(make_settings(tmp_path))
    svc._swap(make_snapshot_df(), "eastmoney")
    row = svc.row("000001")
    from backend.data.quality import check_suspended

    assert check_suspended(row) is True


# ---------- 预计算 ----------

def test_compute_indicators(ohlcv):
    out = compute_indicators(ohlcv)
    for key in ("close", "bars", "ma5", "ma10", "ma20", "ma60", "rsi14", "macd", "macd_signal"):
        assert key in out
    assert 0 <= out["rsi14"] <= 100
    assert out["bars"] == len(ohlcv)
