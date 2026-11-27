"""记忆层：分层摘要触发/事实回读校验/ask_user 挂起/决策结算/反思。"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from backend.engine.askuser import AskUserManager
from backend.memory.decisions import DecisionStore, direction_ok
from backend.memory.reflections import settle_return
from backend.memory.summarize import (estimate_tokens, fact_readback_verify,
                                      keep_recent, parse_summary_json,
                                      render_history, should_summarize,
                                      LayeredSummary)


# ---------- 分层摘要 ----------

def test_estimate_tokens_chinese():
    assert estimate_tokens("一二三四五六七八九十") == 6  # 10 字 × 0.6


def test_should_summarize_triggers():
    assert should_summarize(20001, 1, token_trigger=20000, round_trigger=20) is True
    assert should_summarize(100, 21, token_trigger=20000, round_trigger=20) is True
    assert should_summarize(100, 1, token_trigger=20000, round_trigger=20) is False


def test_keep_recent_split():
    conv = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(30)]
    old, recent = keep_recent(conv, keep_rounds=6)
    assert len(recent) == 12
    assert recent[-1]["content"] == "m29"
    assert "m5" in old and "m29" not in old


def test_keep_recent_short_no_split():
    conv = [{"role": "user", "content": "a"}]
    old, recent = keep_recent(conv, 6)
    assert old == "" and recent == conv


def test_fact_readback_verify_catches_loss():
    """摘要把关键事实"摘丢"→ 补录（6位代码 + 百分比）。"""
    conv = "我们讨论了 600519，目标价涨幅 12.5%，还有 000001 跌幅 3%"
    summary = LayeredSummary(facts=["讨论了两只股票"], conclusions=["偏多"])
    fixed = fact_readback_verify(summary, conv)
    text = " ".join(fixed.facts)
    assert "600519" in text and "000001" in text
    assert "12.5%" in text and "3%" in text


def test_fact_readback_verify_no_change_when_complete():
    conv = "600519 涨了 5%"
    summary = LayeredSummary(facts=["600519 涨了 5%"], conclusions=[])
    fixed = fact_readback_verify(summary, conv)
    assert "补录" not in " ".join(fixed.facts)


def test_parse_summary_json_tolerates_code_fence():
    text = '```json\n{"facts": ["600519"], "conclusions": ["看多"]}\n```'
    s = parse_summary_json(text)
    assert s.facts == ["600519"]
    s2 = parse_summary_json("不是 JSON")
    assert s2.facts  # 容错降级


def test_render_history_structure():
    s = LayeredSummary(facts=["600519"], conclusions=["看多"])
    msgs = render_history(s, [{"role": "user", "content": "hi"}])
    assert msgs[0]["role"] == "system" and "会话摘要" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}


# ---------- ask_user 挂起 ----------

async def test_ask_resume_roundtrip(tmp_db):
    mgr = AskUserManager(tmp_db, timeout_seconds=5)
    pa = await mgr.create("s1", "您能接受多大回撤？", ["A", "B", "C"])
    assert mgr.is_pending("s1")
    assert pa.qid

    task = asyncio.create_task(mgr.wait(pa))
    await asyncio.sleep(0.05)
    ok = await mgr.resume("s1", pa.qid, "B")
    assert ok is True
    answer = await task
    assert answer["choice"] == "B" and answer["timeout"] is False
    assert not mgr.is_pending("s1")


async def test_ask_timeout_cancels(tmp_db):
    mgr = AskUserManager(tmp_db, timeout_seconds=0.2)
    pa = await mgr.create("s2", "问题", ["A", "B"])
    answer = await mgr.wait(pa)
    assert answer["timeout"] is True and answer["choice"] is None


async def test_resume_wrong_qid_rejected(tmp_db):
    mgr = AskUserManager(tmp_db, timeout_seconds=5)
    await mgr.create("s3", "问题", ["A", "B"])
    assert await mgr.resume("s3", "wrong-qid", "A") is False
    assert await mgr.resume("unknown-session", "x", "A") is False


async def test_ask_persisted_to_sqlite(tmp_db):
    mgr = AskUserManager(tmp_db, timeout_seconds=5)
    pa = await mgr.create("s4", "问题", ["A", "B"])
    row = tmp_db.query_one("SELECT * FROM pending_asks WHERE session_id='s4'")
    assert row is not None and row["qid"] == pa.qid
    await mgr.wait(pa)
    row2 = tmp_db.query_one("SELECT state FROM pending_asks WHERE session_id='s4'")
    assert row2["state"] in ("answered", "timeout")


# ---------- 决策记忆与结算 ----------

def test_decision_save_and_history(tmp_db, tmp_path):
    store = DecisionStore(tmp_db, snapshot_dir=str(tmp_path / "snaps"))
    did = store.save_decision("600519", "bullish", "基本面稳健", {"k": 1},
                              "2026-11-01", horizon_days=3)
    assert did > 0
    # 未结算 → history_for 为空
    assert store.history_for("600519") == []
    assert len(store.pending_decisions()) == 1
    store.mark_settled(did, 0.032, "低估了量能衰减")
    hist = store.history_for("600519")
    assert len(hist) == 1
    assert hist[0]["settled"] == pytest.approx(0.032)
    assert hist[0]["reflection"] == "低估了量能衰减"


def test_settle_return_trade_days():
    close = pd.Series([10.0, 10.5, 11.0, 12.0, 11.0, 12.5, 13.0],
                      index=[f"2026-01-0{i}" for i in range(1, 8)])
    ret, day = settle_return(close, "2026-01-01", 3)
    assert ret == pytest.approx(12.0 / 10.0 - 1)
    assert day.startswith("2026-01-04")
    # T+N 不足 → None（等后续结算）
    assert settle_return(close, "2026-01-06", 3) is None
    # 锚日不在序列 → None
    assert settle_return(close, "2025-12-01", 3) is None


def test_direction_ok():
    assert direction_ok("bullish", 0.02) is True
    assert direction_ok("bullish", -0.02) is False
    assert direction_ok("bearish", -0.02) is True
    assert direction_ok("neutral", 0.5) is True   # watch/neutral 不判对错


def test_decision_snapshot_frozen(tmp_path, tmp_db):
    store = DecisionStore(tmp_db, snapshot_dir=str(tmp_path / "snaps"))
    store.save_decision("600519", "bullish", "d", {"datapack": "DATA"}, "2026-11-01", 5)
    hist = store.history_for("600519")
    assert hist == []  # 未结算
    pending = store.pending_decisions()[0]
    from pathlib import Path

    assert Path(pending["snapshot_path"]).exists()
