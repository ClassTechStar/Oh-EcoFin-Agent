"""数据质检：停牌 / 涨跌停三档板 / 缺列 NaN / 新鲜度徽章。"""

from __future__ import annotations

from backend.data.quality import (
    check_limit_hit,
    check_suspended,
    classify_board,
    freshness,
    limit_threshold,
    validate_snapshot_df,
)


def test_suspension_volume_zero():
    assert check_suspended({"volume": 0}) is True
    assert check_suspended({"volume": 12345}) is False
    assert check_suspended({"成交量": 0}) is True
    assert check_suspended(None) is True
    assert check_suspended({"volume": None}) is True


def test_board_classification():
    assert classify_board("600519") == "main"
    assert classify_board("000001") == "main"
    assert classify_board("300750") == "growth"
    assert classify_board("688981") == "growth"
    assert classify_board("000001", "ST某某") == "st"
    assert classify_board("600519", "贵州茅台") == "main"


def test_limit_thresholds_three_tiers():
    """验收：停牌/涨跌停打标单测覆盖三种板块档位。"""
    assert limit_threshold("600519") == 10.0     # 主板
    assert limit_threshold("300750") == 20.0     # 创业板
    assert limit_threshold("600519", "ST某") == 5.0  # ST


def test_limit_hit_flags():
    assert check_limit_hit("600519", 9.95) is not None   # 主板触及
    assert check_limit_hit("600519", 9.0) is None        # 未触及
    assert check_limit_hit("300750", 19.95) is not None  # 创业板
    assert check_limit_hit("300750", 12.0) is None
    assert check_limit_hit("000001", 4.95, "ST") is not None  # ST 档
    assert check_limit_hit("000001", 3.0, "ST") is None
    assert check_limit_hit("600519", None) is None


def test_snapshot_df_validation():
    import pandas as pd

    df = pd.DataFrame({"代码": ["600519"], "名称": ["贵州茅台"], "最新价": [1],
                       "涨跌幅": [0], "成交量": [1]})
    assert validate_snapshot_df(df) == []
    df2 = df.drop(columns=["成交量"])
    assert "成交量" in validate_snapshot_df(df2)
    assert validate_snapshot_df(None) == ["代码", "名称", "最新价", "涨跌幅", "成交量"]


def test_freshness_badges():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone(timedelta(hours=8)))
    assert freshness(now.isoformat(), False)["freshness"] == "green"    # 盘中<90s
    old = (now - timedelta(hours=5)).isoformat()
    assert freshness(old, False)["freshness"] == "yellow"               # 盘后当日
    older = (now - timedelta(days=3)).isoformat()
    assert freshness(older, True)["freshness"] == "red"                 # 隔日/降级
    assert freshness("", True)["freshness"] == "red"
