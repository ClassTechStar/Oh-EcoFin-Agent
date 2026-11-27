"""M10 执行层：G1/G2 闸、PaperBroker A 股规则、OrderRouter 落库与对账。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.db import Database
from backend.execution import (
    AccountContext,
    CapitalGate,
    ExecutionStore,
    GateChain,
    OrderRequest,
    OrderRouter,
    PaperBroker,
    Position,
    RiskDecision,
    SuitabilityGate,
)


def ctx(**kwargs) -> AccountContext:
    base = dict(cash=1_000_000.0, total_equity=1_000_000.0, risk_level="R3")
    base.update(kwargs)
    return AccountContext(**base)


def order(**kwargs) -> OrderRequest:
    base = dict(symbol="600519", side="buy", qty=1000, price=10.0)
    base.update(kwargs)
    return OrderRequest(**base)


# ---------- 订单契约 ----------

def test_order_request_validation():
    o = order(symbol="1")
    assert o.symbol == "000001"
    with pytest.raises(ValidationError):
        OrderRequest(symbol="AAPL", side="buy", qty=100, price=10)
    with pytest.raises(ValidationError):
        OrderRequest(symbol="600519", side="buy", qty=0, price=10)
    with pytest.raises(ValidationError, match="limit_price"):
        OrderRequest(symbol="600519", side="buy", qty=100, price=10,
                     order_type="limit")
    assert order(qty=100).strategy_id == "manual"


# ---------- G1 适当性闸 ----------

def test_g1_r1_forbids_equity():
    d = SuitabilityGate().pre_trade(order(), ctx(risk_level="R1"))
    assert not d.allowed and d.severity == "block"
    assert any("R1" in r for r in d.reasons)


def test_g1_single_cap_reduces_qty():
    positions = {"600519": Position("600519", qty=19_500, available_qty=19_500,
                                    avg_cost=10.0)}
    d = SuitabilityGate().pre_trade(order(qty=1000), ctx(positions=positions))
    # R3 单一上限 20% × 100 万 = 20 万；已占 19.5 万 → 仅剩 500 股
    assert d.allowed and d.adjusted_qty == 500 and d.severity == "warn"


def test_g1_industry_cap_reduces_qty():
    c = ctx(industry={"600519": "liquor"}, industry_caps={"liquor": 0.05})
    d = SuitabilityGate().pre_trade(order(qty=10_000), c)
    assert d.allowed and d.adjusted_qty == 5_000


def test_g1_sell_not_blocked_by_caps():
    c = ctx(risk_level="R1", positions={"600519": Position("600519", 100, 100, 10)})
    d = SuitabilityGate().pre_trade(order(side="sell", qty=100), c)
    assert d.allowed and d.adjusted_qty == 100


# ---------- G2 资金与仓位闸 ----------

def test_g2_lot_rounding_and_cash_cap():
    d = CapitalGate().pre_trade(order(qty=155), ctx())
    assert d.adjusted_qty == 100 and d.severity == "warn"

    poor = ctx(cash=9_000.0, total_equity=9_000.0)
    d2 = CapitalGate().pre_trade(order(qty=1000), poor)
    assert d2.allowed and d2.adjusted_qty == 800  # 800×10+佣金 ≤ 9000


def test_g2_daily_loss_fuse_blocks_buys():
    c = ctx(daily_pnl=-25_000.0, daily_loss_limit=0.02)
    d = CapitalGate().pre_trade(order(), c)
    assert not d.allowed and any("熔断" in r for r in d.reasons)


def test_g2_t_plus_one_available():
    c = ctx(positions={"600519": Position("600519", qty=1000, available_qty=300,
                                          avg_cost=10)})
    d = CapitalGate().pre_trade(order(side="sell", qty=1000), c)
    assert d.allowed and d.adjusted_qty == 300 and d.severity == "warn"

    none = ctx(positions={"600519": Position("600519", qty=1000, available_qty=0,
                                             avg_cost=10)})
    d2 = CapitalGate().pre_trade(order(side="sell", qty=100), none)
    assert not d2.allowed and any("T+1" in r for r in d2.reasons)


class _Greedy:
    name = "greedy"

    def pre_trade(self, req, c):
        return RiskDecision(True, req.qty + 100, [], "pass", self.name)


def test_gate_chain_rejects_increase_and_aggregates():
    chain = GateChain([SuitabilityGate(), CapitalGate()])
    d = chain.evaluate(order(qty=155), ctx())
    assert d.allowed and d.adjusted_qty == 100
    assert any("整手" in r for r in d.reasons)

    with pytest.raises(ValueError, match="加仓"):
        GateChain([_Greedy()]).evaluate(order(), ctx())


# ---------- PaperBroker ----------

def test_paper_broker_buy_t1_and_reconcile():
    broker = PaperBroker(1_000_000)
    result = broker.fill(order(qty=100), 10.0)
    assert result["ok"]
    assert result["cash"] == pytest.approx(999_000 - 5.0)
    assert result["position"]["qty"] == 100
    assert result["position"]["available_qty"] == 0

    ctx_after = broker.context({"600519": 10.0})
    assert ctx_after.position("600519").available_qty == 0
    sell = order(side="sell", qty=100)
    ok, reason = broker.can_fill(sell, 10.0)
    assert not ok and "T+1" in reason

    broker.roll_day()
    assert broker.can_fill(sell, 10.0)[0]
    assert broker.fill(sell, 10.0)["ok"]

    check = broker.reconcile({"600519": 10.0})
    assert check["ok"] and check["fills"] == 2
    assert check["total_fees"] > 0


def test_paper_broker_limits_and_limit_orders():
    broker = PaperBroker(1_000_000)
    res = broker.fill(order(), 10.0, limit_up=10.0)
    assert not res["ok"] and "涨停" in res["reason"]
    res = broker.fill(order(), 10.0, suspended=True)
    assert not res["ok"] and "停牌" in res["reason"]

    limited = order(order_type="limit", limit_price=9.5, qty=100)
    ok, reason = broker.can_fill(limited, 10.0)
    assert not ok and "限价未穿越" in reason
    assert broker.can_fill(limited, 9.4)[0]


def test_paper_broker_reconcile_after_price_move():
    broker = PaperBroker(1_000_000)
    broker.fill(order(qty=100), 10.0)
    check = broker.reconcile({"600519": 12.0})
    assert check["ok"]
    assert check["unrealized_pnl"] == pytest.approx(200.0)
    assert check["equity"] == pytest.approx(1_000_195.0)


# ---------- 路由与落库 ----------

def test_router_persists_orders_and_risk_events(tmp_path):
    db = Database(tmp_path / "exec.db")
    store = ExecutionStore(db)
    broker = PaperBroker(1_000_000)
    router = OrderRouter(GateChain([SuitabilityGate(), CapitalGate()]), broker, store)

    c = broker.context({"600519": 10.0}, risk_level="R3")
    out = router.submit(order(qty=155), c, market_price=10.0)
    assert out["ok"] and out["status"] == "filled"
    assert out["fill"]["qty"] == 100

    orders = store.recent_orders()
    assert orders[0]["status"] == "filled"
    assert orders[0]["risk_decision"]["adjusted_qty"] == 100
    events = store.recent_risk_events()
    assert events and events[0]["level"] in ("pass", "warn")

    # R1 买入 → 拒绝，且落库
    r1 = broker.context({"600519": 10.0}, risk_level="R1")
    bad = router.submit(order(qty=100), r1, market_price=10.0)
    assert not bad["ok"] and bad["status"] == "rejected"
    assert any(o["status"] == "rejected" for o in store.recent_orders())


def test_router_rejects_when_broker_cannot_fill(tmp_path):
    store = ExecutionStore(Database(tmp_path / "exec.db"))
    broker = PaperBroker(1_000_000)
    router = OrderRouter(GateChain([CapitalGate()]), broker, store)
    c = broker.context({"600519": 10.0})
    out = router.submit(order(qty=100), c, market_price=10.0, suspended=True)
    assert not out["ok"] and "停牌" in out["reason"]
    assert any(e["rule"] == "match" and e["level"] == "block"
               for e in store.recent_risk_events())
