"""回测/截断审计落库（M8/M9）：字段完整性 / 指纹确定性 / 泄漏报告锚定。"""

from __future__ import annotations

import pytest

from backend.db import Database
from backend.quant.audit import LeakReport
from backend.quant.audit_store import AuditStore, spec_hash
from backend.quant.backtest import run_backtest
from backend.quant.fixtures import synthetic_ohlcv


@pytest.fixture(scope="module")
def sample_result() -> dict:
    df = synthetic_ohlcv(days=300)
    return run_backtest(df, "600519", 5, 20)


def test_spec_hash_deterministic_and_sensitive():
    a = {"strategy_id": "dual_ma", "params": {"fast": 5, "slow": 20}}
    b = {"params": {"slow": 20, "fast": 5}, "strategy_id": "dual_ma"}
    assert spec_hash(a) == spec_hash(b)
    assert len(spec_hash(a)) == 16
    assert spec_hash(a) != spec_hash({**a, "params": {"fast": 10, "slow": 20}})


def test_save_and_get_backtest(tmp_path, sample_result):
    store = AuditStore(Database(tmp_path / "audit.db"))
    run_id = store.save_backtest(sample_result, run_id="bt-1")
    assert run_id == "bt-1"

    row = store.get_run(run_id)
    assert row is not None
    assert row["strategy_id"] == "dual_ma"
    assert row["start_date"] == sample_result["data_start"]
    assert row["end_date"] == sample_result["data_asof"]
    assert row["metrics"]["trade_count"] == sample_result["metrics"]["trade_count"]
    assert row["cost_model"]["stamp_duty"] == 0.0005
    assert row["cost_model"]["lot_size"] == 100
    assert "limit_hit" in row["skipped"]
    assert row["provider"] == "dataframe"
    assert row["spec_hash"] and len(row["spec_hash"]) == 16

    assert [r["id"] for r in store.list_runs(limit=5)] == ["bt-1"]
    assert store.get_run("missing") is None


def test_failed_backtest_rejected(tmp_path):
    store = AuditStore(Database(tmp_path / "audit.db"))
    with pytest.raises(ValueError):
        store.save_backtest({"ok": False, "reason": "K线数据为空"})


def test_save_and_list_leaks(tmp_path):
    store = AuditStore(Database(tmp_path / "audit.db"))
    report = LeakReport(symbol="600519", strategy="dual_ma", cut_date="2026-06-02",
                        passed=False, fills_before_cut=12,
                        leaks=["成交序列第 1 笔不一致：完整区间=... 截断区间=..."])
    leak_id = store.save_leak("bt-leak", report)
    assert leak_id > 0

    rows = store.list_leaks("bt-leak")
    assert len(rows) == 1
    assert rows[0]["passed"] is False
    assert rows[0]["trades_before_cut"] == 12
    assert rows[0]["cut_date"] == "2026-06-02"
    assert rows[0]["leaks"] and "不一致" in rows[0]["leaks"][0]
    assert store.list_leaks("other") == []
