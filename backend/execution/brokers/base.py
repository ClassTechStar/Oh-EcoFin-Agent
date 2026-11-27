"""券商适配器契约：QMT / 券商 OpenAPI 网关实现同一组异步方法。

安全纪律：
- 默认关闭（`execution.live_enabled=False`），未配置账户/路径时 fail-closed；
- 凭据只从配置/环境读取，日志不打印 token 与完整账号；
- 下单返回 `GatewayAck(status="accepted")`，成交以券商回报为准，绝不伪装成交。
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from backend.execution.gateway import GatewayAck
from backend.execution.models import AccountContext, OrderRequest

logger = logging.getLogger(__name__)


@runtime_checkable
class BrokerAdapter(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def health(self) -> bool: ...

    async def submit(self, order: OrderRequest, ctx: AccountContext, *,
                     market_price: float,
                     limit_up: float | None = None,
                     limit_down: float | None = None,
                     suspended: bool = False) -> GatewayAck: ...

    async def cancel(self, broker_order_id: str) -> bool: ...


def stock_code(symbol: str) -> str:
    """6 位代码 → QMT 证券代码（.SH/.SZ/.BJ）。"""
    sym = str(symbol).zfill(6)
    if sym.startswith(("6", "68", "9")):
        return f"{sym}.SH"
    if sym.startswith(("4", "8")):
        return f"{sym}.BJ"
    return f"{sym}.SZ"


def mask_account(account_id: str) -> str:
    """账号脱敏：前后各保留 3 位。"""
    account = str(account_id or "")
    if len(account) <= 6:
        return "***" if account else ""
    return f"{account[:3]}***{account[-3:]}"
