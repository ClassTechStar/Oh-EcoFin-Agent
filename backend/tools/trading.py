"""交易工具面（M7/M10）：信号合成 / 仿真账户 / 仿真下单，供 Agent 调用。

纪律：
- 风控不可绕过：下单仍走 `OrderRouter`（G1/G2），工具层不做任何例外；
- 成交价由服务端快照决定，模型只能给方向与数量；
- 工具结果做压缩（模型上下文有限），但保留弃权原因、风控拒绝原因与成交明细。
"""

from __future__ import annotations

import logging
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.execution.service import (
    InvalidRiskLevelError,
    QuoteUnavailableError,
    account_payload,
    submit_paper_order,
    validate_risk_level,
)
from backend.signals.service import SignalDataError, preview_signals

logger = logging.getLogger(__name__)


def _norm_symbol(v: str) -> str:
    sym = str(v).strip().zfill(6)
    if not sym.isdigit():
        raise ValueError(f"非法股票代码: {v}")
    return sym


class SignalToolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6 位 A 股代码")
    mode: Literal["long_only", "long_short", "dollar_neutral"] = Field(
        "long_only", description="组合模式；A 股默认纯多头")
    gross: float = Field(0.8, gt=0, le=1, description="目标总仓位（0-1）")

    @field_validator("symbol")
    @classmethod
    def _v(cls, v: str) -> str:
        return _norm_symbol(v)


class AccountToolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: str = Field("R3", description="风险等级 R1-R5（决定权益/单一上限）")


class PaperOrderToolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6 位 A 股代码")
    side: Literal["buy", "sell"]
    qty: int = Field(..., ge=1, le=100_000, description="委托股数（买入须整手）")
    risk_level: str = Field("R3", description="风险等级 R1-R5")
    strategy_id: str = Field("agent", description="策略标识（审计用）")
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(None, gt=0, description="限价单价格")

    @field_validator("symbol")
    @classmethod
    def _v(cls, v: str) -> str:
        return _norm_symbol(v)


class TradingTools:
    """同步服务之上的异步工具封装；`day_open_equity` 用回调读取最新值。"""

    def __init__(self, market, signal_store, router, broker, snapshot,
                 day_open_equity: Callable[[], float]):
        self.market = market
        self.signal_store = signal_store
        self.router = router
        self.broker = broker
        self.snapshot = snapshot
        self._day_open_equity = day_open_equity

    async def get_alpha_signals(self, symbol: str, mode: str = "long_only",
                                gross: float = 0.8) -> dict:
        try:
            payload = await preview_signals(
                self.market, self.signal_store, symbol, mode=mode, gross=gross)
        except SignalDataError as e:
            return {"ok": False, "error": str(e),
                    "hint": "确认标的代码、数据源与样本长度后重试"}
        return {
            "ok": True,
            "symbol": payload["symbol"],
            "as_of": payload["as_of"],
            "mode": payload["mode"],
            "gross_target": payload["gross_target"],
            "weights": payload["weights"],
            "raw_weights": payload["raw_weights"],
            "flat_reason": payload["flat_reason"],
            "signals": [{
                "model": s["model_name"],
                "value": s["value"],
                "confidence": s["confidence"],
                "horizon_days": s["horizon_days"],
                "abstained": s["abstained"],
                "reason": s["reason"],
                "evidence": (s["evidence"][0]["excerpt"] if s["evidence"] else ""),
            } for s in payload["signals"]],
            "failures": payload["failures"],
            "saved_signals": payload["saved_signals"],
            "disclaimer": "信号为量化模型观点，不构成投资建议。",
        }

    async def get_paper_account(self, risk_level: str = "R3") -> dict:
        try:
            level = validate_risk_level(risk_level)
        except InvalidRiskLevelError as e:
            return {"ok": False, "error": str(e)}
        payload = account_payload(self.broker, self.snapshot, level,
                                  self._day_open_equity())
        return {"ok": True, **payload}

    async def submit_paper_order(self, symbol: str, side: str, qty: int,
                                 risk_level: str = "R3", strategy_id: str = "agent",
                                 order_type: str = "market",
                                 limit_price: float | None = None) -> dict:
        try:
            result = await submit_paper_order(
                self.router, self.broker, self.snapshot,
                day_open_equity=self._day_open_equity(),
                symbol=symbol, side=side, qty=qty, risk_level=risk_level,
                strategy_id=strategy_id, order_type=order_type,
                limit_price=limit_price)
        except (QuoteUnavailableError, InvalidRiskLevelError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        order = result["order"]
        return {
            "ok": result["ok"],
            "order_id": order.get("order_id"),
            "status": order.get("status"),
            "fill": order.get("fill"),
            "reason": order.get("reason", ""),
            "decision": order.get("decision", {}),
            "account": {
                "cash": result["account"]["cash"],
                "equity": result["account"]["equity"],
                "positions": result["account"]["positions"],
                "reconcile_ok": result["account"]["reconcile"]["ok"],
            },
            "quote": result["quote"],
            "disclaimer": "仿真成交，不涉及真实资金；不构成投资建议。",
        }
