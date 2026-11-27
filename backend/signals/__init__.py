"""信号层：AlphaModel 契约、确定性组合合成、内置量化模型。

设计来源（代码级移植，非照搬）：
- `AlphaModel` 契约：ai-hedge-fund `hedge_fund/signals/base.py:17-47`
  —— 权限元数据（investment_approach）+ 观点周期（horizon_days）+ abstain 纪律；
- `blend_signals` 合成：ai-hedge-fund `hedge_fund/portfolio/construction.py:29-105`
  —— 加权投票、空头证据权限、flat_reason 显式化。

本项目强化点：
1. 每个信号强制携带 `evidence`（工具/参数/时间/摘录），与投研报告的 `[n]` 溯源同构；
2. `abstained=True` 时 value 必须为 0（用校验器强制，防止"弃权但偷偷投票"）；
3. 合成过程为纯函数、零 LLM、零随机，可单测可复现。
"""

from backend.signals.base import (
    AlphaModel,
    AlphaSignal,
    DataContext,
    EvidenceRef,
    InvestmentApproach,
    SignalRegistry,
    registry,
)
from backend.signals.builtin import (
    LowVolatilityAlpha,
    MomentumAlpha,
    ShortReversalAlpha,
    default_models,
)
from backend.signals.constraints import (
    ConstraintPipeline,
    GroupExposure,
    MaxWeight,
    MinWeight,
    TurnoverCap,
    WeightConstraint,
    apply_to_weights,
)
from backend.signals.construction import BlendResult, FlatReason, blend_signals
from backend.signals.context import MarketDataContext
from backend.signals.pipeline import (
    ComposeResult,
    SignalRun,
    compose,
    run_models,
)
from backend.signals.store import SignalStore

__all__ = [
    "AlphaModel", "AlphaSignal", "DataContext", "EvidenceRef",
    "InvestmentApproach", "SignalRegistry", "registry",
    "BlendResult", "FlatReason", "blend_signals",
    "MomentumAlpha", "ShortReversalAlpha", "LowVolatilityAlpha", "default_models",
    "ConstraintPipeline", "GroupExposure", "MaxWeight", "MinWeight",
    "TurnoverCap", "WeightConstraint", "apply_to_weights",
    "MarketDataContext", "SignalRun", "ComposeResult", "compose", "run_models",
    "SignalStore",
]
