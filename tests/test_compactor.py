"""压缩器：K线/快照/新闻/财务/回测 降维 + golden 对照。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.engine.compactor import (
    compact_backtest,
    compact_financial,
    compact_kline,
    compact_news,
    compact_quote,
    label_sentiment,
    pack,
)

GOLDEN = Path(__file__).resolve().parent / "golden"


def sample_kline_bars(n=500):
    from tests.helpers import synthetic_ohlcv

    df = synthetic_ohlcv(n)
    return [{"date": r["date"], "open": round(float(r["open"]), 2),
             "high": round(float(r["high"]), 2), "low": round(float(r["low"]), 2),
             "close": round(float(r["close"]), 2), "volume": float(r["volume"])}
            for _, r in df.iterrows()]


def test_compact_kline_stats():
    out = compact_kline({"bars": sample_kline_bars(), "data_as_of": "2026-11-27 10:00:00"})
    assert out["ok"] is True
    for key in ("last_close", "period_pct", "trend", "ann_vol_pct",
                "ma20_dist_pct", "ma60_dist_pct", "supports", "resistances", "volume_note"):
        assert key in out
    assert len(out["supports"]) <= 5
    assert len(out["resistances"]) <= 5


def test_compact_kline_token_budget():
    """K线压缩 ≤300 tokens（中文 0.6 tokens/字符粗估）。"""
    out = compact_kline({"bars": sample_kline_bars()})
    size = len(json.dumps(out, ensure_ascii=False))
    assert size * 0.6 <= 300, f"kline 压缩超预算: {size} chars"


def test_compact_kline_empty():
    assert compact_kline({"bars": []})["ok"] is False


def test_compact_quote_six_fields():
    out = compact_quote({"quote": {"最新价": 1520.5, "涨跌幅": 1.35, "量比": 1.2,
                                   "换手率": 0.55, "总市值": 19100.0, "市盈率-动态": 22.1,
                                   "市净率": 8.9}})
    assert out["ok"] is True
    for key in ("price", "pct_chg", "volume_ratio", "turnover", "market_cap", "pe", "pb"):
        assert key in out


def test_compact_news_dedup_and_sentiment():
    titles = ["净利润大增 超预期", "净利润大增 超预期", "股价大跌 利空",
              "公司中标大单", "平淡公告一则", "回购计划发布", "立案调查",
              "新高铁", "分红到账", "亏损扩大"]
    items = [{"title": t, "source": "s", "published_at": "2026-11-27T09:00:00"} for t in titles]
    out = compact_news({"news": items})
    assert out["kept"] <= 5
    assert out["aggregate_sentiment"] <= 1.0 and out["aggregate_sentiment"] >= -1.0
    assert all(x["sentiment"] in ("bullish", "bearish", "neutral") for x in out["items"])


def test_label_sentiment():
    assert label_sentiment("业绩超预期大涨") == "bullish"
    assert label_sentiment("遭立案调查 利空") == "bearish"
    assert label_sentiment("今日发布公告") == "neutral"


def test_compact_financial():
    out = compact_financial({"indicators": [{"报告期": "2026-09-30", "营业收入": 100.0,
                                             "同比增长": 12.5, "净利润": 50.0}]})
    assert out["ok"] is True
    assert out["items"], "应保留关键项"


def test_compact_backtest_metrics():
    out = compact_backtest({"metrics": {"total_return": 0.12, "sharpe": 1.5,
                                        "max_drawdown": -0.08, "win_rate": 0.55,
                                        "profit_loss_ratio": 1.8, "trade_count": 12}})
    assert out["metrics"]["夏普"] == 1.5
    assert out["metrics"]["最大回撤"] == -0.08


def test_pack_dispatcher_unknown_tool_raw():
    r = {"custom": 1}
    assert pack("unknown_tool", r) is r


def test_golden_compactor_samples():
    """压缩前后样本对照固化（防优化时悄悄改变语义）。"""
    golden_file = GOLDEN / "compactor_kline.json"
    sample = {"bars": sample_kline_bars(120)}
    out = compact_kline(sample)
    if not golden_file.exists():
        GOLDEN.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return  # 首次运行生成 golden
    golden = json.loads(golden_file.read_text(encoding="utf-8"))
    # 结构字段一致（数值允许因随机种子固定而完全一致）
    for key in ("trend", "volume_note", "bars_count"):
        assert out[key] == golden[key], f"golden 语义漂移: {key}"
    for key in ("ma20", "ma60", "last_close", "period_pct"):
        assert out[key] == golden[key]
