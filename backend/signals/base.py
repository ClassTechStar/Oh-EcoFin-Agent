"""AlphaModel 契约与信号数据结构。

契约三条铁律（与 ai-hedge-fund 同源，本仓库强化）：
1. **权限与观点分离**：`investment_approach` 是模型能力权限（能否做空），
   不是观点方向；只有 long_short 模型的负向观点才能生成空头证据。
2. **周期显式**：`horizon_days` 声明观点兑现周期，评分与再平衡据此对齐，
   禁止用同一周期评价价值型与事件型模型。
3. **弃权诚实**：无法形成观点时置 `abstained=True` 且 `value=0`，
   绝不以"中性"伪装"无观点"（否则会稀释他人信号）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

InvestmentApproach = Literal["long_only", "long_short"]


class EvidenceRef(BaseModel):
    """一条可核验的证据（与投研报告 [n] 引用同构）。"""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(1, ge=1, description="证据编号")
    tool: str = Field(..., description="产生该证据的工具/方法名")
    args: str = Field("", description="调用参数（可复现）")
    ts: str = Field(..., description="数据截至时间")
    excerpt: str = Field("", description="关键数值摘录（供人工复核）")


class AlphaSignal(BaseModel):
    """单个模型对单个标的的点时观点。"""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    symbol: str
    as_of: str = Field(..., description="观点时点（YYYY-MM-DD），只用该日及以前的数据")
    value: float = Field(..., ge=-1.0, le=1.0, description="[-1,+1]，0 为中性")
    confidence: float = Field(0.5, ge=0.0, le=1.0,
                              description="排序用置信度；不等于校准概率")
    horizon_days: int = Field(5, ge=1, description="观点兑现周期（交易日）")
    approach: InvestmentApproach = "long_only"
    evidence: list[EvidenceRef] = Field(default_factory=list)
    abstained: bool = False
    reason: str = Field("", description="弃权原因（abstained=True 时必填）")

    @model_validator(mode="after")
    def _abstain_is_zero(self) -> AlphaSignal:
        """弃权必须零观点：防止「弃权但偷偷投票」污染合成。"""
        if self.abstained:
            if abs(self.value) > 1e-9:
                raise ValueError("abstained=True 时 value 必须为 0")
            if not self.reason:
                raise ValueError("abstained=True 时必须给出 reason")
        return self


class DataContext(Protocol):
    """模型取数上下文（点位只读，禁止未来数据）。

    实现方需保证：`end_date` 之后的数据不可见。回测场景由调用方切片传入。
    """

    async def daily_bars(self, symbol: str, days: int = 120,
                         end_date: str | None = None) -> pd.DataFrame:
        """日线（date/open/high/low/close/volume，升序）。"""
        ...

    async def quote(self, symbol: str) -> dict:
        """最新快照（含 price/pct_chg/name）。"""
        ...


class AlphaModel(ABC):
    """所有 Alpha 模型的统一基类。"""

    name: ClassVar[str] = "base"
    investment_approach: ClassVar[InvestmentApproach] = "long_only"
    horizon_days: ClassVar[int] = 5

    @abstractmethod
    async def predict(self, symbol: str, as_of: str, ctx: DataContext) -> AlphaSignal:
        """形成点时观点；数据不足必须 abstain，不得抛异常中断管道。"""

    # ---- 便捷构造 ----

    def make_signal(self, *, symbol: str, as_of: str, value: float,
                    confidence: float = 0.6,
                    evidence: list[EvidenceRef] | None = None,
                    horizon_days: int | None = None) -> AlphaSignal:
        clipped = max(-1.0, min(1.0, float(value)))
        return AlphaSignal(
            model_name=self.name, symbol=symbol, as_of=as_of,
            value=round(clipped, 4), confidence=round(float(confidence), 4),
            horizon_days=horizon_days or self.horizon_days,
            approach=self.investment_approach,
            evidence=evidence or [],
        )

    def abstain(self, symbol: str, as_of: str, reason: str) -> AlphaSignal:
        return AlphaSignal(
            model_name=self.name, symbol=symbol, as_of=as_of,
            value=0.0, confidence=0.0, horizon_days=self.horizon_days,
            approach=self.investment_approach, abstained=True, reason=reason,
        )


class SignalRegistry:
    """模型注册表（名称唯一，禁止静默覆盖）。"""

    def __init__(self) -> None:
        self._models: dict[str, AlphaModel] = {}

    def register(self, model: AlphaModel) -> None:
        if model.name in self._models:
            raise ValueError(f"AlphaModel 重复注册: {model.name}")
        self._models[model.name] = model

    def get(self, name: str) -> AlphaModel:
        if name not in self._models:
            raise KeyError(f"未注册的 AlphaModel: {name}")
        return self._models[name]

    def all(self) -> list[AlphaModel]:
        return list(self._models.values())

    def names(self) -> list[str]:
        return list(self._models)


#: 进程级默认注册表（内置模型在 builtin.default_models 中注册）
registry = SignalRegistry()
