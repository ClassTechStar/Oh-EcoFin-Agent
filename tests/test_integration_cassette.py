"""卡带回放集成：DEMO_MODE 冻结卡带走完整工具链（CI 零密钥零网络零成本）。"""

from __future__ import annotations

import asyncio
import json
import zlib
from pathlib import Path

import pandas as pd
import pytest

from backend.data.calendar import TradeCalendar
from backend.data.snapshot import SnapshotService
from backend.tools.market import MarketTools
from backend.tools.research import ResearchTools
from backend.data.precompute import PrecomputeService
from backend.data.ratelimit import TokenBucket
from tests.helpers import synthetic_ohlcv

CASSETTES = Path(__file__).resolve().parent / "cassettes_frozen"


@pytest.fixture(scope="module")
def cassette_dir(tmp_path_factory):
    """构造冻结卡带：snapshot.json + klines/*.json + financial/*.json。"""
    d = tmp_path_factory.mktemp("cassettes")
    (d / "klines").mkdir()
    (d / "financial").mkdir()
    symbols = ["600519", "000001", "000858"]
    snap_rows = []
    for i, sym in enumerate(symbols):
        df = synthetic_ohlcv(500, seed=zlib.crc32(sym.encode()) % 1000)
        (d / "klines" / f"{sym}.json").write_text(
            json.dumps(df.to_dict(orient="records"), default=str), encoding="utf-8")
        (d / "financial" / f"{sym}.json").write_text(json.dumps({
            "indicators": [{"报告期": "2026-09-30", "营业收入": 100 + i,
                            "同比增长": 5.5, "净利润": 50 + i, "同比增长": 8.0}]
        }, ensure_ascii=False), encoding="utf-8")
        snap_rows.append({
            "代码": sym, "名称": f"演示股{i}", "最新价": round(float(df["close"].iloc[-1]), 2),
            "涨跌幅": 1.2 + i * 0.3, "成交量": 123456.0, "换手率": 0.6,
            "量比": 1.1, "总市值": 1000.0 + i, "市盈率-动态": 20.0, "市净率": 5.0,
        })
    (d / "snapshot.json").write_text(json.dumps(snap_rows, ensure_ascii=False),
                                     encoding="utf-8")
    return d


def make_settings(cassette_dir):
    s = type("S", (), {})()
    s.data = type("D", (), {"rate_limit_rps": 1.0, "rate_limit_burst": 3,
                            "kline_cache_ttl": 3600, "news_cache_ttl": 1800,
                            "notice_cache_ttl": 600,
                            "snapshot_interval_intraday": 60,
                            "snapshot_interval_idle": 1800})()
    s.app = type("A", (), {"demo_mode": True})()
    s.cassette_dir = str(cassette_dir)
    s.search = type("SC", (), {"enabled_sources": ["bocha"], "news_max_hours": 24})()
    return s


@pytest.fixture()
def wired(cassette_dir):
    settings = make_settings(cassette_dir)
    cal = TradeCalendar(cache_path=cassette_dir / "trade_dates.json")
    snap = SnapshotService(settings, cal)
    ok = asyncio.run(snap.refresh_once())
    assert ok is True
    market = MarketTools(snapshot=snap, bucket=TokenBucket(100, 10),
                         kline_cache_ttl=3600, cassette_dir=str(cassette_dir),
                         demo_mode=True)
    pre = PrecomputeService()
    research = ResearchTools(market, None, pre)
    return market, snap, pre, research


async def test_cassette_get_quote(wired):
    market, snap, _, _ = wired
    out = await market.get_quote(["600519", "000858"])
    assert out["ok"] is True
    assert len(out["quotes"]) == 2
    q = out["quotes"][0]
    assert q["name"].startswith("演示股")
    assert out["elapsed_ms"] < 50  # 快照命中 <50ms 红线（合成数据上应远低于）


async def test_cassette_get_kline(wired):
    market, _, _, _ = wired
    out = await market.get_kline("600519", 120)
    assert out["ok"] is True
    assert len(out["bars"]) == 120
    assert out["bars"][0]["date"] < out["bars"][-1]["date"]


async def test_cassette_get_financial(wired):
    _, _, _, research = wired
    out = await research.get_financial("600519")
    assert out["ok"] is True
    assert out["indicators"][0]["营业收入"] == 100


async def test_cassette_get_valuation(wired):
    _, _, _, research = wired
    out = await research.get_valuation("600519")
    assert out["ok"] is True
    assert "price_percentile_3y" in out


async def test_cassette_get_indicator_and_precompute(wired):
    market, snap, pre, research = wired
    pre.set_fetcher(market.get_kline_raw)
    await pre.precompute(["600519"], days=120)
    out = await research.get_indicator("600519")
    assert out["ok"] is True
    assert "ma20" in out["indicator"] or out["indicator"]


async def test_cassette_correlation(wired):
    _, _, _, research = wired
    out = await research.get_correlation(["600519", "000001", "000858"])
    assert out["ok"] is True
    assert len(out["matrix"]["symbols"]) == 3
    n = len(out["matrix"]["values"])
    # 对角线为 1
    assert out["matrix"]["values"][0][0] == pytest.approx(1.0)
    assert n == 3


async def test_cassette_snapshot_stale_semantics(wired):
    """DEMO 卡带 → is_stale=True 诚实降级。"""
    _, snap, _, _ = wired
    assert snap.freshness()["is_stale"] is True
    assert snap._source == "cassette"
