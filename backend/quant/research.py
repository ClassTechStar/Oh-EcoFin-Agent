"""策略自动迭代流水线（P3）：候选策略 → 回测 → walk-forward → 截断审计 → 晋升门禁。

约束（与报告 6.4 风险登记对齐）：
- 任何一步失败/缺证据 → 不晋升（fail-closed），绝不"重跑直到出现偏好结论"；
- 多标的组合逐标的审计，任一标的前视偏差 → 整体否决；
- 指标聚合口径显式：夏普/收益/回撤取等权均值，成交笔数求和（便于对照）；
- 晋升决策可写入 `promotions` 表并追加治理账本（哈希链）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from backend.governance.ledger import Ledger
from backend.quant.audit import LeakReport, check_strategy
from backend.quant.audit_store import AuditStore
from backend.quant.backtest import run_backtest
from backend.quant.promotion import (
    PromotionDecision,
    PromotionPolicy,
    evaluate_promotion,
)
from backend.quant.walkforward import walk_forward

logger = logging.getLogger(__name__)


class StrategySpec(BaseModel):
    """候选策略声明（参数 + 标的池 + 组合约束）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = "0.1.0"
    universe: list[str] = Field(..., min_length=1)
    alpha_models: list[str] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    rebalance: str = "daily"
    constraints: list[dict] = Field(default_factory=list)

    @property
    def spec_hash_payload(self) -> dict:
        return {"id": self.id, "version": self.version,
                "universe": sorted(self.universe), "alpha_models": self.alpha_models,
                "params": self.params, "rebalance": self.rebalance,
                "constraints": self.constraints}


@dataclass
class CandidateReport:
    spec: dict
    backtest: dict
    walk_forward: dict
    leak: LeakReport
    metrics: dict
    decision: PromotionDecision
    per_symbol: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "metrics": self.metrics,
            "walk_forward": {"n_folds": self.walk_forward.get("n_folds", 0),
                             "avg_decay": self.walk_forward.get("avg_decay", 0),
                             "folds": self.walk_forward.get("folds", [])},
            "leak": self.leak.to_dict(),
            "decision": self.decision.to_dict(),
            "per_symbol": self.per_symbol,
        }


def _aggregate_metrics(results: list[dict]) -> dict:
    n = len(results)
    return {
        "total_return": round(sum(r["metrics"]["total_return"] for r in results) / n, 4),
        "sharpe": round(sum(r["metrics"]["sharpe"] for r in results) / n, 3),
        "max_drawdown": round(sum(r["metrics"]["max_drawdown"] for r in results) / n, 4),
        "win_rate": round(sum(r["metrics"]["win_rate"] for r in results) / n, 4),
        "trade_count": sum(r["metrics"]["trade_count"] for r in results),
    }


def _merge_leaks(reports: list[LeakReport]) -> LeakReport:
    failing = [r for r in reports if not r.passed]
    chosen = failing[0] if failing else reports[0]
    skipped = next((r for r in reports if r.skipped_reason and r.passed), None)
    leaks = [f"{r.symbol}: {msg}" for r in reports for msg in r.leaks]
    return LeakReport(
        symbol=",".join(r.symbol for r in reports),
        strategy=chosen.strategy,
        cut_date=chosen.cut_date,
        passed=all(r.passed for r in reports),
        fills_before_cut=sum(r.fills_before_cut for r in reports),
        equity_points_compared=sum(r.equity_points_compared for r in reports),
        leaks=leaks,
        skipped_reason=(skipped.skipped_reason if (skipped and all(r.passed for r in reports))
                        else ""),
    )


def evaluate_candidate(spec: StrategySpec,
                       data_by_symbol: dict[str, pd.DataFrame], *,
                       ic_t: float | None = None,
                       daily_turnover: float | None = None,
                       max_folds: int = 4,
                       policy: PromotionPolicy | None = None) -> CandidateReport:
    """逐标的回测 + 审计 + walk-forward，任一标的缺数据即 ValueError。"""
    missing = [s for s in spec.universe if s not in data_by_symbol]
    if missing:
        raise ValueError(f"候选策略 {spec.id} 缺少标的行情: {missing}")

    params = dict(spec.params or {})
    backtests, wfs, leaks, per_symbol = [], [], [], []
    for symbol in spec.universe:
        df = data_by_symbol[symbol]
        bt = run_backtest(df, symbol, **params)
        if not bt.get("ok"):
            raise ValueError(f"{symbol} 回测失败: {bt.get('reason', '')}")
        bt_params = dict(bt.get("params") or {})
        runner = (lambda d, s=symbol, p=bt_params: run_backtest(d, s, **p))
        leak = check_strategy(df, symbol, cut_ratio=0.7, runner=runner)
        wf = walk_forward(df, symbol, max_folds=max_folds)
        backtests.append(bt)
        leaks.append(leak)
        wfs.append(wf)
        per_symbol.append({"symbol": symbol, "metrics": bt["metrics"],
                           "leak": leak.to_dict(),
                           "avg_decay": wf.get("avg_decay")})

    merged_leak = _merge_leaks(leaks)
    all_folds = [dict(f) for wf in wfs for f in wf.get("folds", [])]
    aggregate_wf = {"ok": bool(all_folds), "folds": all_folds,
                    "n_folds": len(all_folds),
                    "avg_decay": round(
                        sum(float(f.get("decay") or 0) for f in all_folds)
                        / len(all_folds), 3) if all_folds else 0.0}
    metrics = _aggregate_metrics(backtests)

    decision = evaluate_promotion(
        strategy_id=f"{spec.id}@{spec.version}",
        leak=merged_leak, wf=aggregate_wf, metrics=metrics,
        ic_t=ic_t, daily_turnover=daily_turnover,
        data_start=min(bt["data_start"] for bt in backtests),
        data_asof=max(bt["data_asof"] for bt in backtests),
        policy=policy)

    return CandidateReport(spec=spec.model_dump(), backtest=backtests[0],
                           walk_forward=aggregate_wf, leak=merged_leak,
                           metrics=metrics, decision=decision,
                           per_symbol=per_symbol)


def promote_candidate(report: CandidateReport, *,
                      store: AuditStore | None = None,
                      ledger: Ledger | None = None,
                      backtest_id: str = "",
                      spec_hash: str = "") -> dict:
    """落库晋升决策 + 追加治理账本（仅记录，不改变 approved）。"""
    if ledger is not None:
        ledger.append("promotion", report.decision.strategy_id,
                      {"approved": report.decision.approved,
                       "version": report.spec.get("version"),
                       "gates": [g.to_dict() for g in report.decision.gates]})
    promo_id = None
    if store is not None:
        promo_id = store.save_promotion(report.decision, spec_hash=spec_hash,
                                        backtest_id=backtest_id)
    return {"promotion_id": promo_id, "approved": report.decision.approved,
            "decision": report.decision.to_dict()}
