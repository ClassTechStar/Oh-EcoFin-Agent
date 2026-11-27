"""策略晋升门禁（P3）：截断审计 + 样本外夏普 + IC + 换手 + 回撤 + 数据覆盖。

六道闸全部由确定性代码判定（LLM 无权批准）：
| 闸 | 规则 | 一票否决 |
|---|---|---|
| leak | `passed=True` 且非 skipped | 是 |
| oos_sharpe | 均值 > 0.5 且 ≥ 样本内 30% | 否 |
| ic | \\|t\\| > 2（多期截面 IC） | 否 |
| turnover | 日均 < 30% | 否 |
| drawdown | 最大回撤 < 15% | 否 |
| coverage | 回测区间 ≥ 3 年 | 否 |

缺证据 = 不通过（fail-closed）：没有截断报告、没有 IC 面板、没有换手统计，
一律不允许晋升，避免「证据缺失被当成默许」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from backend.quant.audit import LeakReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromotionPolicy:
    min_oos_sharpe: float = 0.5
    min_decay_ratio: float = 0.3
    min_ic_t: float = 2.0
    max_daily_turnover: float = 0.30
    max_drawdown: float = 0.15
    min_history_years: float = 3.0


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str
    veto: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "detail": self.detail, "veto": self.veto}


@dataclass
class PromotionDecision:
    strategy_id: str
    approved: bool
    gates: list[GateResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "approved": self.approved,
                "gates": [g.to_dict() for g in self.gates],
                "reasons": list(self.reasons)}


def _years(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    days = (d1 - d0).days
    return days / 365.25 if days > 0 else None


def _leak_gate(leak: LeakReport | None) -> GateResult:
    if leak is None:
        return GateResult("leak", False, "缺少截断检验报告", veto=True)
    if not leak.passed:
        detail = "；".join(leak.leaks[:2]) or "截断前后成交/权益不一致"
        return GateResult("leak", False,
                          f"检出前视偏差（cut={leak.cut_date}）：{detail}", veto=True)
    if leak.skipped_reason:
        return GateResult("leak", False,
                          f"检验无判别力（skipped）：{leak.skipped_reason}", veto=True)
    return GateResult("leak", True,
                      f"截断前后一致（cut={leak.cut_date}，"
                      f"比对成交 {leak.fills_before_cut} 笔）")


def _sharpe_gate(wf: dict | None, policy: PromotionPolicy) -> GateResult:
    folds = (wf or {}).get("folds") or []
    if not folds:
        return GateResult("oos_sharpe", False, "缺少 walk-forward 分折结果")
    oos = sum(float(f.get("oos_sharpe") or 0.0) for f in folds) / len(folds)
    is_ = sum(float(f.get("is_sharpe") or 0.0) for f in folds) / len(folds)
    decay = (oos / is_) if is_ > 1e-9 else 0.0
    ok = oos > policy.min_oos_sharpe and (
        is_ <= 1e-9 or decay >= policy.min_decay_ratio)
    return GateResult(
        "oos_sharpe", ok,
        f"样本外夏普均值 {oos:.2f}（门槛 >{policy.min_oos_sharpe}），"
        f"衰减率 {decay:.0%}（门槛 ≥{policy.min_decay_ratio:.0%}）")


def _ic_gate(ic_t: float | None, policy: PromotionPolicy) -> GateResult:
    if ic_t is None:
        return GateResult("ic", False, "缺少多期 IC t 统计量")
    return GateResult("ic", abs(ic_t) > policy.min_ic_t,
                      f"IC t={ic_t:.2f}（门槛 |t|>{policy.min_ic_t}）")


def _turnover_gate(turnover: float | None, policy: PromotionPolicy) -> GateResult:
    if turnover is None:
        return GateResult("turnover", False, "缺少换手统计")
    return GateResult("turnover", 0 <= turnover < policy.max_daily_turnover,
                      f"日均换手 {turnover:.1%}（门槛 <{policy.max_daily_turnover:.0%}）")


def _drawdown_gate(metrics: dict | None, policy: PromotionPolicy) -> GateResult:
    mdd = (metrics or {}).get("max_drawdown")
    if mdd is None:
        return GateResult("drawdown", False, "缺少回测回撤指标")
    value = abs(float(mdd))
    return GateResult("drawdown", value < policy.max_drawdown,
                      f"最大回撤 {value:.1%}（门槛 <{policy.max_drawdown:.0%}）")


def _coverage_gate(start: str | None, end: str | None,
                   policy: PromotionPolicy) -> GateResult:
    years = _years(start, end)
    if years is None:
        return GateResult("coverage", False, "缺少回测区间起止日期")
    return GateResult("coverage", years >= policy.min_history_years,
                      f"回测区间 {years:.1f} 年（门槛 ≥{policy.min_history_years:.0f} 年）")


def evaluate_promotion(*, strategy_id: str,
                       leak: LeakReport | None,
                       wf: dict | None,
                       metrics: dict | None = None,
                       ic_t: float | None = None,
                       daily_turnover: float | None = None,
                       data_start: str | None = None,
                       data_asof: str | None = None,
                       policy: PromotionPolicy | None = None) -> PromotionDecision:
    """执行六道闸；全部通过才批准，任一否决闸失败直接否决。"""
    policy = policy or PromotionPolicy()
    gates = [
        _leak_gate(leak),
        _sharpe_gate(wf, policy),
        _ic_gate(ic_t, policy),
        _turnover_gate(daily_turnover, policy),
        _drawdown_gate(metrics, policy),
        _coverage_gate(data_start, data_asof, policy),
    ]
    approved = all(g.passed for g in gates)
    reasons = [f"[{g.name}] {g.detail}" for g in gates if not g.passed]
    if not approved and any(g.veto for g in gates if not g.passed):
        reasons.insert(0, "一票否决：截断检验未通过或无判别力")
    decision = PromotionDecision(strategy_id=strategy_id, approved=approved,
                                 gates=gates, reasons=reasons)
    logger.info("策略 %s 晋升评估: %s", strategy_id,
                "approved" if approved else "rejected")
    return decision
