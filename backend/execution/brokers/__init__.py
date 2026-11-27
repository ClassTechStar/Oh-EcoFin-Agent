"""实盘券商适配器入口：默认关闭，显式配置后才构建（fail-closed）。"""

from __future__ import annotations

import logging

from backend.config import Settings
from backend.execution.brokers.base import BrokerAdapter, mask_account, stock_code
from backend.execution.brokers.http_api import HttpBrokerAdapter
from backend.execution.brokers.qmt import QmtBrokerAdapter
from backend.execution.gateway import LiveGateway

logger = logging.getLogger(__name__)

__all__ = [
    "BrokerAdapter", "HttpBrokerAdapter", "QmtBrokerAdapter",
    "build_live_gateway", "mask_account", "stock_code",
]


def build_live_gateway(settings: Settings) -> LiveGateway | None:
    """按配置构建实盘网关；未开启或配置不完整时返回 None（不假装可用）。"""
    cfg = settings.execution
    if not cfg.live_enabled:
        return None
    kind = (cfg.broker_kind or "").strip().lower()
    if kind == "qmt":
        adapter = QmtBrokerAdapter(cfg)
    elif kind in ("http", "openapi"):
        adapter = HttpBrokerAdapter(cfg)
    else:
        raise ValueError(f"未知券商适配器类型: {cfg.broker_kind!r}")
    logger.info("实盘通道已装配: %s（账户 %s）", kind, mask_account(cfg.account_id))
    return LiveGateway(adapter)
