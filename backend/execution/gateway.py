"""执行网关契约（M10/P2）：统一提交接口 + 幂等重连；未配置券商时诚实失败。

P2 验收要求「Gateway 断线重连 10 次无丢单」：
- 客户端订单号（`OrderRequest.id`）即幂等键，重连后重发不会产生重复成交；
- 网关层不持有任何风控例外——`PaperGateway` 内部仍走 `OrderRouter`（G1/G2 不可绕过）；
- `LiveGateway` 未注入券商适配器时抛 `BrokerNotConfigured`，绝不伪造成交。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from backend.execution.models import AccountContext, OrderRequest

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """网关基础异常。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class GatewayDisconnectedError(GatewayError):
    """连接断开（可重试；幂等键保证重试不重复成交）。"""


class BrokerNotConfiguredError(GatewayError):
    """实盘通道未配置（诚实失败，不降级为仿真）。"""


@dataclass
class GatewayAck:
    client_order_id: str
    broker_order_id: str
    status: str                     # filled | rejected | accepted
    fill: dict | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"client_order_id": self.client_order_id,
                "broker_order_id": self.broker_order_id,
                "status": self.status, "fill": self.fill, "reason": self.reason}


@runtime_checkable
class ExecutionGateway(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def health(self) -> bool: ...

    async def submit(self, order: OrderRequest, ctx: AccountContext, *,
                     market_price: float,
                     limit_up: float | None = None,
                     limit_down: float | None = None,
                     suspended: bool = False) -> GatewayAck: ...

    async def cancel(self, broker_order_id: str) -> bool: ...


class PaperGateway:
    """仿真网关：内部路由 + 按 client_order_id 幂等。"""

    name = "paper"

    def __init__(self, router):
        self.router = router
        self._acks: dict[str, GatewayAck] = {}
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def health(self) -> bool:
        return self._connected

    async def submit(self, order: OrderRequest, ctx: AccountContext, *,
                     market_price: float,
                     limit_up: float | None = None,
                     limit_down: float | None = None,
                     suspended: bool = False) -> GatewayAck:
        if order.id in self._acks:            # 幂等：重发返回首次结果
            return self._acks[order.id]
        result = self.router.submit(order, ctx, market_price=market_price,
                                    limit_up=limit_up, limit_down=limit_down,
                                    suspended=suspended)
        ack = GatewayAck(
            client_order_id=order.id,
            broker_order_id=(f"paper-{order.id}" if result.get("ok") else ""),
            status=result.get("status", "rejected"),
            fill=result.get("fill"), reason=result.get("reason", ""),
        )
        self._acks[order.id] = ack
        return ack

    async def cancel(self, broker_order_id: str) -> bool:
        # 仿真为即时成交，无挂单可撤
        return False


class ResilientGateway:
    """重连包装：断线 → 重连 → 用同一幂等键重发，直到成功或耗尽重试。"""

    name = "resilient"

    def __init__(self, inner: ExecutionGateway, *, max_retries: int = 5,
                 backoff: float = 0.0):
        if max_retries < 0:
            raise ValueError("max_retries 不能为负")
        self.inner = inner
        self.max_retries = int(max_retries)
        self.backoff = float(backoff)
        self.reconnects = 0
        self.acks: dict[str, GatewayAck] = {}

    async def connect(self, *, force: bool = False) -> None:
        if force:
            self.reconnects += 1
        await self.inner.connect()

    async def health(self) -> bool:
        return await self.inner.health()

    async def submit(self, order: OrderRequest, ctx: AccountContext, *,
                     market_price: float,
                     limit_up: float | None = None,
                     limit_down: float | None = None,
                     suspended: bool = False) -> GatewayAck:
        if order.id in self.acks:
            return self.acks[order.id]
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                await self.connect(force=attempt > 0)
                ack = await self.inner.submit(
                    order, ctx, market_price=market_price, limit_up=limit_up,
                    limit_down=limit_down, suspended=suspended)
                self.acks[order.id] = ack
                return ack
            except GatewayDisconnectedError as e:
                last = e
                logger.warning("网关断线（第 %d 次），订单 %s 将重试", attempt + 1, order.id)
                if self.backoff:
                    await asyncio.sleep(self.backoff)
        raise GatewayDisconnectedError(f"重试 {self.max_retries} 次仍失败: {last}")

    async def cancel(self, broker_order_id: str) -> bool:
        return await self.inner.cancel(broker_order_id)


class LiveGateway:
    """实盘通道占位：未注入券商适配器时拒绝一切操作（fail-closed）。"""

    name = "live"

    def __init__(self, adapter=None):
        self.adapter = adapter

    def _require(self):
        if self.adapter is None:
            raise BrokerNotConfiguredError(
                "未配置券商适配器：实盘阶段需注入合规券商 Gateway，禁止用仿真通道冒充")
        return self.adapter

    async def connect(self) -> None:
        await self._require().connect()

    async def health(self) -> bool:
        return bool(await self._require().health())

    async def submit(self, order: OrderRequest, ctx: AccountContext, **kwargs) -> GatewayAck:
        return await self._require().submit(order, ctx, **kwargs)

    async def cancel(self, broker_order_id: str) -> bool:
        return await self._require().cancel(broker_order_id)
