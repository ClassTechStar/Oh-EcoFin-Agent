"""P2：网关幂等重连 / 影子账户 20 日对账 / G3 盘中闸。"""

from __future__ import annotations

import pytest

from backend.execution import (
    BrokerNotConfiguredError,
    CapitalGate,
    GateChain,
    GatewayAck,
    GatewayDisconnectedError,
    IntradayGate,
    LiveGateway,
    OrderRequest,
    OrderRouter,
    PaperBroker,
    PaperGateway,
    ResilientGateway,
    ShadowAccount,
    SuitabilityGate,
    Tick,
)
from backend.execution.gateway import ExecutionGateway


class FlakyGateway:
    """前 N 次提交在「券商已受理但回包丢失」后断线（最危险的重复下单场景）。"""

    name = "flaky"

    def __init__(self, disconnect_times: int = 10):
        self.remaining = disconnect_times
        self.accepted: dict[str, GatewayAck] = {}
        self.submit_calls = 0
        self.connects = 0
        self.health_calls = 0

    async def connect(self) -> None:
        self.connects += 1

    async def health(self) -> bool:
        self.health_calls += 1
        return True

    async def submit(self, order, ctx, **kwargs) -> GatewayAck:
        self.submit_calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            if order.id not in self.accepted:
                self.accepted[order.id] = GatewayAck(order.id, "b-lost", "filled",
                                                     {"qty": order.qty})
            raise GatewayDisconnectedError("回包丢失")
        if order.id in self.accepted:
            return self.accepted[order.id]
        ack = GatewayAck(order.id, "b-ok", "filled", {"qty": order.qty})
        self.accepted[order.id] = ack
        return ack

    async def cancel(self, broker_order_id: str) -> bool:
        return False


def _order() -> OrderRequest:
    return OrderRequest(symbol="600519", side="buy", qty=100, price=10.0)


def _ctx(broker: PaperBroker):
    return broker.context({"600519": 10.0})


async def test_resilient_gateway_ten_disconnects_no_duplicate():
    inner = FlakyGateway(disconnect_times=10)
    gw = ResilientGateway(inner, max_retries=12)
    ack = await gw.submit(_order(), _ctx(PaperBroker()), market_price=10.0)
    assert ack.status == "filled"
    assert inner.submit_calls == 11          # 10 次断线 + 1 次成功
    assert len(inner.accepted) == 1          # 幂等键保证只下一单
    assert gw.reconnects == 10
    # 再次提交同一订单 → 直接返回首次 ack，不触达网关
    again = await gw.submit(_order(), _ctx(PaperBroker()), market_price=10.0)
    assert again.status == "filled"


async def test_resilient_gateway_exhausted_raises():
    gw = ResilientGateway(FlakyGateway(disconnect_times=99), max_retries=2)
    with pytest.raises(GatewayDisconnectedError):
        await gw.submit(_order(), _ctx(PaperBroker()), market_price=10.0)


async def test_paper_gateway_idempotent():
    broker = PaperBroker(1_000_000)
    router = OrderRouter(GateChain([SuitabilityGate(), CapitalGate()]), broker)
    gw = PaperGateway(router)
    assert isinstance(gw, ExecutionGateway)
    await gw.connect()
    assert await gw.health()
    order = _order()
    first = await gw.submit(order, _ctx(broker), market_price=10.0)
    second = await gw.submit(order, _ctx(broker), market_price=10.0)
    assert first.status == "filled"
    assert second is first
    assert len(broker.fills) == 1
    assert await gw.cancel("paper-x") is False


async def test_live_gateway_fails_closed_without_adapter():
    gw = LiveGateway()
    with pytest.raises(BrokerNotConfiguredError):
        await gw.connect()
    with pytest.raises(BrokerNotConfiguredError):
        await gw.submit(_order(), _ctx(PaperBroker()), market_price=10.0)
    with pytest.raises(BrokerNotConfiguredError):
        await gw.cancel("x")


# ---------- G3 盘中闸 ----------

def test_intraday_suspended_and_limits():
    gate = IntradayGate()
    actions = gate.on_tick(Tick("600519", last=10.0, prev_close=10.0, suspended=True))
    assert actions[0].rule == "suspended" and actions[0].level == "block"
    blocked = gate.blocked_symbols(actions)
    assert "600519" in blocked["buy"] and "600519" in blocked["sell"]

    up = gate.on_tick(Tick("600519", last=11.0, prev_close=10.0, amount=2e7))
    assert any(a.rule == "limit_up" for a in up)
    # 创业板 20%：11 元 / 10 元未触及
    cyb = gate.on_tick(Tick("300750", last=11.0, prev_close=10.0, amount=2e7))
    assert not any(a.rule == "limit_up" for a in cyb)

    down = gate.on_tick(Tick("600519", last=9.0, prev_close=10.0, amount=2e7))
    assert any(a.rule == "limit_down" for a in down)


def test_intraday_liquidity_and_portfolio_fuse():
    gate = IntradayGate(min_amount=5_000_000, turbulence_limit=0.1)
    actions = gate.on_tick(Tick("600519", last=10.0, prev_close=10.0, amount=1e6))
    assert any(a.rule == "low_liquidity" and a.level == "warn" for a in actions)
    halt = gate.on_tick(Tick("600519", last=10.0, prev_close=10.0, amount=8e6),
                        portfolio_return=-0.12)
    assert any(a.rule == "portfolio_circuit_breaker" and a.level == "halt" for a in halt)


# ---------- 影子账户 ----------

def test_shadow_account_twenty_days_reconciles():
    broker = PaperBroker(1_000_000)
    router = OrderRouter(GateChain([SuitabilityGate(), CapitalGate()]), broker)
    shadow = ShadowAccount(router, broker)

    for i in range(20):
        day = f"2026-11-{i + 1:02d}"
        prices = {"600519": 10.0 if i == 0 else 10.4}
        shadow.start_day(day, prices)
        if i == 0:
            out = shadow.submit(OrderRequest(symbol="600519", side="buy",
                                             qty=100, price=10.0), prices)
            assert out["ok"]
        if i == 1:
            out = shadow.submit(OrderRequest(symbol="600519", side="sell",
                                             qty=100, price=10.4), prices)
            assert out["ok"]
        entry = shadow.end_day(prices)
        assert entry.reconcile_ok, entry

    summary = shadow.summary()
    assert summary["days"] == 20
    assert summary["breaches"] == []
    assert summary["fills"] == 2
    assert summary["final_equity"] > 1_000_000  # 价差 40 元 − 费用
    assert len(shadow.equity_curve) == 20


def test_shadow_account_counts_blocked_orders():
    broker = PaperBroker(1_000_000)
    router = OrderRouter(GateChain([SuitabilityGate(), CapitalGate()]), broker)
    shadow = ShadowAccount(router, broker, risk_level="R1")
    prices = {"600519": 10.0}
    shadow.start_day("2026-11-02", prices)
    out = shadow.submit(OrderRequest(symbol="600519", side="buy",
                                     qty=100, price=10.0), prices)
    assert not out["ok"]
    entry = shadow.end_day(prices)
    assert entry.blocked == 1 and entry.reconcile_ok
