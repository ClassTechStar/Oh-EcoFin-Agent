"""仿真执行服务：REST 看板与 Agent 工具共用（价格由服务端快照决定）。"""

from __future__ import annotations

import asyncio

from backend.data.quality import check_suspended
from backend.execution.models import OrderRequest
from backend.quant.backtest import limit_pct_for
from backend.quant.compliance import EQUITY_CAPS, SINGLE_CAPS


class QuoteUnavailableError(ValueError):
    """快照无该标的/无有效价格（不允许前端自带价格冒充）。"""


class InvalidRiskLevelError(ValueError):
    """未知风险等级。"""


def validate_risk_level(raw: str) -> str:
    level = str(raw).strip().upper()
    if level not in EQUITY_CAPS:
        raise InvalidRiskLevelError(f"未知风险等级: {raw}")
    return level


def symbol_quote(snapshot, symbol: str) -> dict:
    row = snapshot.row(symbol)
    if row is None:
        raise QuoteUnavailableError(
            f"快照无 {symbol}：请等待快照刷新，或使用 DEMO 卡带启动")
    try:
        price = float(row.get("最新价"))
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        raise QuoteUnavailableError(f"{symbol} 无有效最新价")
    try:
        pct = float(row.get("涨跌幅") or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    name = str(row.get("名称") or "")
    prev_close = price / (1 + pct / 100) if pct > -100 else price
    limit = limit_pct_for(symbol, name)
    return {
        "symbol": symbol,
        "name": name,
        "price": round(price, 2),
        "pct_chg": pct,
        "prev_close": round(prev_close, 4),
        "limit_up": round(prev_close * (1 + limit / 100), 2),
        "limit_down": round(prev_close * (1 - limit / 100), 2),
        "suspended": check_suspended(row),
        "data_as_of": snapshot.as_of,
    }


def positions_prices(broker, snapshot) -> dict[str, float]:
    prices: dict[str, float] = {}
    for sym in broker.positions:
        row = snapshot.row(sym)
        price = 0.0
        if row is not None:
            try:
                price = float(row.get("最新价") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
        prices[sym] = price if price > 0 else broker.positions[sym].avg_cost
    return prices


def account_context(broker, snapshot, risk_level: str, day_open_equity: float):
    prices = positions_prices(broker, snapshot)
    equity = broker.context(prices).total_equity
    return broker.context(
        prices, risk_level=risk_level,
        daily_pnl=equity - day_open_equity, daily_loss_limit=0.02)


def account_payload(broker, snapshot, risk_level: str, day_open_equity: float) -> dict:
    prices = positions_prices(broker, snapshot)
    equity = broker.context(prices).total_equity
    recon = broker.reconcile(prices)
    positions = []
    for sym, pos in broker.positions.items():
        if pos.qty <= 0:
            continue
        price = prices.get(sym, pos.avg_cost)
        positions.append({
            "symbol": sym,
            "qty": pos.qty,
            "available_qty": pos.available_qty,
            "avg_cost": round(pos.avg_cost, 4),
            "price": round(price, 2),
            "market_value": round(pos.qty * price, 2),
            "pnl": round((price - pos.avg_cost) * pos.qty, 2),
            "pnl_pct": round(price / pos.avg_cost - 1, 4) if pos.avg_cost else 0.0,
        })
    return {
        "risk_level": risk_level,
        "initial_cash": round(broker.initial_cash, 2),
        "cash": round(broker.cash, 2),
        "market_value": recon["market_value"],
        "equity": equity,
        "daily_pnl": round(equity - day_open_equity, 2),
        "position_ratio": round(1 - broker.cash / equity, 4) if equity else 0.0,
        "positions": positions,
        "reconcile": recon,
        "fills": len(broker.fills),
        "caps": {"equity": EQUITY_CAPS, "single": SINGLE_CAPS},
    }


async def submit_paper_order(router, broker, snapshot, *, day_open_equity: float,
                             symbol: str, side: str, qty: int,
                             risk_level: str = "R3", strategy_id: str = "manual",
                             order_type: str = "market",
                             limit_price: float | None = None) -> dict:
    risk = validate_risk_level(risk_level)
    quote = symbol_quote(snapshot, symbol)
    order = OrderRequest(
        symbol=symbol, side=side, qty=int(qty), strategy_id=strategy_id,
        price=quote["price"], order_type=order_type, limit_price=limit_price)
    acct = account_context(broker, snapshot, risk, day_open_equity)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: router.submit(
            order, acct, market_price=quote["price"],
            limit_up=quote["limit_up"], limit_down=quote["limit_down"],
            suspended=quote["suspended"]))
    return {
        "ok": result.get("ok", False),
        "order": result,
        "quote": quote,
        "account": account_payload(broker, snapshot, risk, day_open_equity),
    }
