"""miniQMT（xtquant）实盘适配器：Windows 客户端 + 券商账户，延迟导入 SDK。

软件依赖说明：xtquant 无法通过 pip 安装，需安装券商 QMT/miniQMT 客户端后
将其 `bin.x64/Lib/site-packages` 加入 PYTHONPATH。未安装/未配置时一律
`BrokerNotConfiguredError`，绝不静默降级为仿真。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from backend.config import ExecutionConfig
from backend.execution.brokers.base import stock_code
from backend.execution.gateway import (
    BrokerNotConfiguredError,
    GatewayAck,
    GatewayDisconnectedError,
    GatewayError,
)
from backend.execution.models import AccountContext, OrderRequest

logger = logging.getLogger(__name__)


class QmtBrokerAdapter:
    """miniQMT 交易适配器（同步 SDK 调用放入线程池）。"""

    name = "qmt"

    def __init__(self, config: ExecutionConfig):
        self.config = config
        self._trader = None
        self._account = None

    # ---------- 连接 ----------

    def _validate(self) -> None:
        if not self.config.account_id:
            raise BrokerNotConfiguredError(
                "未配置券商资金账号（FINAGENT_EXECUTION__ACCOUNT_ID）")
        if not self.config.qmt_path:
            raise BrokerNotConfiguredError(
                "未配置 miniQMT 路径（FINAGENT_EXECUTION__QMT_PATH）")

    def _connect_sync(self) -> None:
        self._validate()
        path = Path(self.config.qmt_path)
        if not path.exists():
            raise BrokerNotConfiguredError(f"miniQMT 路径不存在: {path}")
        try:
            from xtquant import xtconstant  # noqa: F401
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as e:
            raise BrokerNotConfiguredError(
                "未安装 miniQMT xtquant（需券商 QMT 客户端，非 pip 包）",
                hint="安装 miniQMT 后将 bin.x64/Lib/site-packages 加入 PYTHONPATH",
            ) from e

        session_id = self.config.qmt_session_id or int(time.time())
        trader = XtQuantTrader(str(path), int(session_id))
        trader.start()
        code = trader.connect()
        if code != 0:
            raise GatewayDisconnectedError(f"QMT 连接失败: code={code}")
        account = StockAccount(self.config.account_id)
        sub = trader.subscribe(account)
        if sub != 0:
            raise GatewayDisconnectedError(f"QMT 账户订阅失败: code={sub}")
        self._trader = trader
        self._account = account
        logger.info("QMT 已连接（账户 %s***）", self.config.account_id[:3])

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _health_sync(self) -> bool:
        if self._trader is None or self._account is None:
            return False
        try:
            return self._trader.query_stock_asset(self._account) is not None
        except Exception as e:
            logger.warning("QMT 健康检查失败: %s", e)
            return False

    async def health(self) -> bool:
        return await asyncio.to_thread(self._health_sync)

    # ---------- 交易 ----------

    def _submit_sync(self, order: OrderRequest, market_price: float) -> GatewayAck:
        if self._trader is None or self._account is None:
            raise GatewayDisconnectedError("QMT 未连接：先 connect 再下单")
        from xtquant import xtconstant

        side = (xtconstant.STOCK_BUY if order.side == "buy"
                else xtconstant.STOCK_SELL)
        if order.order_type == "limit" and order.limit_price:
            price_type = xtconstant.FIX_PRICE
            price = float(order.limit_price)
        else:
            price_type = xtconstant.MARKET_PEER_PRICE_FIRST
            price = float(market_price)
        try:
            broker_order_id = self._trader.order_stock(
                self._account, stock_code(order.symbol), side, int(order.qty),
                price_type, price, order.strategy_id or "oh-ecofin", order.id)
        except Exception as e:
            raise GatewayError(f"QMT 下单异常: {type(e).__name__}: {e}") from e
        if broker_order_id is None or int(broker_order_id) <= 0:
            return GatewayAck(order.id, "", "rejected",
                              reason=f"QMT 拒单: code={broker_order_id}")
        return GatewayAck(order.id, str(broker_order_id), "accepted")

    async def submit(self, order: OrderRequest, ctx: AccountContext, *,
                     market_price: float,
                     limit_up: float | None = None,
                     limit_down: float | None = None,
                     suspended: bool = False) -> GatewayAck:
        return await asyncio.to_thread(self._submit_sync, order, market_price)

    def _cancel_sync(self, broker_order_id: str) -> bool:
        if self._trader is None or self._account is None:
            raise GatewayDisconnectedError("QMT 未连接")
        try:
            code = self._trader.cancel_order_stock(
                self._account, int(broker_order_id))
        except (TypeError, ValueError) as e:
            raise GatewayError(f"非法券商委托号: {broker_order_id}") from e
        return code == 0

    async def cancel(self, broker_order_id: str) -> bool:
        return await asyncio.to_thread(self._cancel_sync, broker_order_id)
