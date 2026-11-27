"""执行层（M10）：订单模型 / G1-G2 风控闸 / PaperBroker / 路由与审计落库。"""

from backend.execution.gates import CapitalGate, GateChain, SuitabilityGate
from backend.execution.gateway import (
    BrokerNotConfiguredError,
    ExecutionGateway,
    GatewayAck,
    GatewayDisconnectedError,
    GatewayError,
    LiveGateway,
    PaperGateway,
    ResilientGateway,
)
from backend.execution.intraday import IntradayGate, RiskAction, Tick
from backend.execution.models import (
    AccountContext,
    OrderRequest,
    Position,
    RiskDecision,
    RiskGate,
    estimate_fees,
    lot_floor,
)
from backend.execution.paper import Fill, PaperBroker
from backend.execution.router import OrderRouter
from backend.execution.shadow import ShadowAccount, ShadowDay
from backend.execution.store import ExecutionStore

__all__ = [
    "AccountContext", "OrderRequest", "Position", "RiskDecision", "RiskGate",
    "estimate_fees", "lot_floor",
    "SuitabilityGate", "CapitalGate", "GateChain",
    "Fill", "PaperBroker", "OrderRouter", "ExecutionStore",
    "GatewayError", "GatewayDisconnectedError", "BrokerNotConfiguredError",
    "GatewayAck",
    "ExecutionGateway", "PaperGateway", "ResilientGateway", "LiveGateway",
    "IntradayGate", "RiskAction", "Tick",
    "ShadowAccount", "ShadowDay",
]
