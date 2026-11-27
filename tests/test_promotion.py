"""P3：晋升门禁六道闸 / 缺证据 fail-closed / 决策落库 / 治理账本哈希链。"""

from __future__ import annotations

import pytest

from backend.db import Database
from backend.governance import GENESIS, Ledger
from backend.quant.audit import LeakReport
from backend.quant.audit_store import AuditStore
from backend.quant.promotion import (
    PromotionPolicy,
    evaluate_promotion,
)


def leak(passed: bool = True, skipped: str = "") -> LeakReport:
    return LeakReport(symbol="600519", strategy="dual_ma", cut_date="2026-06-02",
                      passed=passed, fills_before_cut=20,
                      leaks=[] if passed else ["成交序列第 1 笔不一致"],
                      skipped_reason=skipped)


def wf(is_sharpe: float = 2.0, oos_sharpe: float = 1.0) -> dict:
    return {"ok": True, "folds": [
        {"fold_id": i, "is_sharpe": is_sharpe, "oos_sharpe": oos_sharpe,
         "decay": oos_sharpe / is_sharpe} for i in range(3)
    ]}


def decision(**kwargs):
    base = dict(
        strategy_id="dual_ma-v2", leak=leak(), wf=wf(),
        metrics={"max_drawdown": -0.08}, ic_t=3.1, daily_turnover=0.12,
        data_start="2022-01-01", data_asof="2026-06-01",
    )
    base.update(kwargs)
    return evaluate_promotion(**base)


def test_all_gates_pass():
    d = decision()
    assert d.approved
    assert len(d.gates) == 6
    assert d.reasons == []
    assert {g.name for g in d.gates} == {
        "leak", "oos_sharpe", "ic", "turnover", "drawdown", "coverage"}


def test_leak_veto_overrides_everything():
    d = decision(leak=leak(passed=False))
    assert not d.approved
    assert d.gates[0].veto and not d.gates[0].passed
    assert any("一票否决" in r for r in d.reasons)


def test_skipped_leak_is_fail_closed():
    d = decision(leak=leak(skipped="截断前无成交，检验无判别力"))
    assert not d.approved
    assert any("无判别力" in r for r in d.reasons)


def test_missing_evidence_fails_closed():
    d = decision(leak=None, wf=None, metrics=None, ic_t=None,
                 daily_turnover=None, data_start=None, data_asof=None)
    assert not d.approved
    assert len(d.reasons) >= 6  # 六道闸全部因缺证据而失败


def test_oos_sharpe_and_decay():
    assert not decision(wf=wf(is_sharpe=2.0, oos_sharpe=0.4)).approved
    assert not decision(wf=wf(is_sharpe=2.0, oos_sharpe=0.55)).approved  # 衰减 27.5% < 30%
    assert decision(wf=wf(is_sharpe=1.0, oos_sharpe=0.6)).approved


def test_ic_turnover_drawdown_coverage():
    assert not decision(ic_t=1.5).approved
    assert not decision(daily_turnover=0.45).approved
    assert not decision(metrics={"max_drawdown": -0.22}).approved
    assert not decision(data_start="2024-06-01", data_asof="2026-06-01").approved


def test_custom_policy():
    strict = PromotionPolicy(min_oos_sharpe=1.5, max_drawdown=0.05)
    assert not decision(policy=strict).approved
    assert decision(wf=wf(1.0, 1.6), metrics={"max_drawdown": -0.02},
                    policy=strict).approved


def test_promotion_store_roundtrip(tmp_path):
    db = Database(tmp_path / "promo.db")
    store = AuditStore(db)
    d = decision()
    promo_id = store.save_promotion(d, spec_hash="abc123", backtest_id="bt-1")
    assert promo_id > 0

    row = store.latest_promotion("dual_ma-v2")
    assert row is not None and row["approved"] is True
    assert row["spec_hash"] == "abc123" and row["backtest_id"] == "bt-1"
    assert len(row["gates"]) == 6 and row["reasons"] == []

    rejected = decision(leak=leak(passed=False))
    store.save_promotion(rejected)
    latest = store.latest_promotion("dual_ma-v2")
    assert latest["approved"] is False and latest["reasons"]


def test_ledger_hash_chain_and_tamper_detection(tmp_path):
    db = Database(tmp_path / "ledger.db")
    ledger = Ledger(db)
    assert ledger.verify()["ok"] and ledger.verify()["count"] == 0

    first = ledger.append("promotion", "dual_ma-v2", {"approved": True, "sharpe": 1.1})
    second = ledger.append("promotion", "dual_ma-v3", {"approved": False})
    assert first["prev_hash"] == GENESIS
    assert second["prev_hash"] == first["payload_hash"]
    assert ledger.verify()["ok"]

    # 篡改 payload → 必须被定位
    db.execute("UPDATE ledger SET payload_json=? WHERE seq=?",
               ('{"approved": true, "sharpe": 9.9}', first["seq"]))
    check = ledger.verify()
    assert not check["ok"] and check["broken_at"] == first["seq"]
    assert "篡改" in check["reason"]


def test_ledger_rejects_empty_refs(tmp_path):
    ledger = Ledger(Database(tmp_path / "ledger.db"))
    with pytest.raises(ValueError):
        ledger.append("", "x", {})
