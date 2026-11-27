"""P3 自动迭代：候选策略评估 → 六道闸 → 晋升落库 + 治理账本。"""

from __future__ import annotations

import pytest

from backend.db import Database
from backend.governance import Ledger
from backend.quant.audit_store import AuditStore
from backend.quant.fixtures import synthetic_ohlcv
from backend.quant.research import StrategySpec, evaluate_candidate, promote_candidate


def _data() -> dict:
    return {"600519": synthetic_ohlcv(days=700, seed=7),
            "000001": synthetic_ohlcv(days=700, seed=11)}


def _spec() -> StrategySpec:
    return StrategySpec(id="dual_ma", version="2.0",
                        universe=["600519", "000001"],
                        params={"fast": 5, "slow": 20},
                        rebalance="daily",
                        constraints=[{"type": "max_weight", "cap": 0.25}])


def test_evaluate_candidate_runs_all_gates():
    report = evaluate_candidate(_spec(), _data(), ic_t=2.5, daily_turnover=0.1,
                                max_folds=1)
    out = report.to_dict()
    assert {g["name"] for g in out["decision"]["gates"]} == {
        "leak", "oos_sharpe", "ic", "turnover", "drawdown", "coverage"}
    assert out["leak"]["passed"] is True
    assert out["leak"]["fills_before_cut"] > 0
    assert out["walk_forward"]["n_folds"] >= 1
    assert out["metrics"]["trade_count"] >= 1
    assert len(out["per_symbol"]) == 2
    assert out["spec"]["id"] == "dual_ma"
    assert out["decision"]["approved"] == report.decision.approved


def test_evaluate_candidate_missing_data_fails_fast():
    spec = _spec()
    with pytest.raises(ValueError, match="缺少标的行情"):
        evaluate_candidate(spec, {"600519": synthetic_ohlcv(days=700)})


def test_promote_candidate_persists_and_appends_ledger(tmp_path):
    db = Database(tmp_path / "research.db")
    report = evaluate_candidate(_spec(), _data(), ic_t=2.5, daily_turnover=0.1,
                                max_folds=1)
    result = promote_candidate(report, store=AuditStore(db), ledger=Ledger(db),
                               backtest_id="bt-x", spec_hash="hash-1")
    assert result["promotion_id"] > 0
    assert result["approved"] == report.decision.approved

    stored = AuditStore(db).latest_promotion(report.decision.strategy_id)
    assert stored is not None and stored["spec_hash"] == "hash-1"
    assert len(stored["gates"]) == 6

    check = Ledger(db).verify()
    assert check["ok"] and check["count"] == 1
