"""券商 OpenAPI 网关适配器（通用 HTTP 契约）。

约定（由券商/中间件实现）：
- `GET  {base_url}/health` → 200
- `POST {base_url}/orders` → {"broker_order_id": "...", "status": "accepted|filled|rejected",
                             "fill": {...}?, "reason": "..."}
- `DELETE {base_url}/orders/{broker_order_id}` → 200/404

鉴权：`Authorization: Bearer <http_token>`（token 仅从配置读取，日志不输出）。
网络异常/5xx/429 → `GatewayDisconnectedError`（可重试，靠 client_order_id 幂等）；
4xx → `GatewayError`（不可重试，需人工检查）。
"""

from __future__ import annotations

import logging

import httpx

from backend.config import ExecutionConfig
from backend.execution.gateway import (
    BrokerNotConfiguredError,
    GatewayAck,
    GatewayDisconnectedError,
    GatewayError,
)
from backend.execution.models import AccountContext, OrderRequest

logger = logging.getLogger(__name__)


class HttpBrokerAdapter:
    name = "http"

    def __init__(self, config: ExecutionConfig, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.http_base_url.rstrip("/") if config.http_base_url else "",
            headers={"Authorization": f"Bearer {config.http_token}"} if config.http_token else {},
            timeout=float(config.timeout_seconds or 10.0),
            transport=transport,
        )

    def _require_config(self) -> None:
        if not self.config.http_base_url:
            raise BrokerNotConfiguredError(
                "未配置券商网关地址（FINAGENT_EXECUTION__HTTP_BASE_URL）")

    async def connect(self) -> None:
        self._require_config()
        try:
            resp = await self._client.get("/health")
        except httpx.HTTPError as e:
            raise GatewayDisconnectedError(f"券商网关不可达: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise GatewayDisconnectedError(
                f"券商网关健康检查失败: HTTP {resp.status_code}")

    async def health(self) -> bool:
        if not self.config.http_base_url:
            return False
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def submit(self, order: OrderRequest, ctx: AccountContext, *,
                     market_price: float,
                     limit_up: float | None = None,
                     limit_down: float | None = None,
                     suspended: bool = False) -> GatewayAck:
        self._require_config()
        payload = {
            "client_order_id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "qty": order.qty,
            "order_type": order.order_type,
            "price": market_price,
            "limit_price": order.limit_price,
            "strategy_id": order.strategy_id,
        }
        try:
            resp = await self._client.post("/orders", json=payload)
        except httpx.HTTPError as e:
            raise GatewayDisconnectedError(
                f"券商网关下单网络异常（可重试）: {type(e).__name__}") from e
        if resp.status_code in (429,) or resp.status_code >= 500:
            raise GatewayDisconnectedError(
                f"券商网关限流/故障: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise GatewayError(f"券商网关拒单: HTTP {resp.status_code} "
                               f"{resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise GatewayError("券商网关返回非 JSON") from e
        status = str(data.get("status") or "accepted")
        return GatewayAck(
            client_order_id=order.id,
            broker_order_id=str(data.get("broker_order_id") or ""),
            status=status,
            fill=data.get("fill"),
            reason=str(data.get("reason") or ""),
        )

    async def cancel(self, broker_order_id: str) -> bool:
        self._require_config()
        try:
            resp = await self._client.delete(f"/orders/{broker_order_id}")
        except httpx.HTTPError as e:
            raise GatewayDisconnectedError(
                f"券商网关撤单网络异常: {type(e).__name__}") from e
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise GatewayError(f"券商网关撤单失败: HTTP {resp.status_code}")
        return True
