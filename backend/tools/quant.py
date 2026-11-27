"""量化工具封装：回测 / Walk-forward / 多因子（线程池执行重型计算）。"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.data.ratelimit import TokenBucket
from backend.quant.backtest import run_backtest
from backend.quant.factors import factor_ic_table
from backend.quant.walkforward import walk_forward

logger = logging.getLogger(__name__)


class BacktestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6位A股代码")
    fast: int = Field(5, ge=5, le=20, description="快线周期（双均线）")
    slow: int = Field(20, ge=20, le=120, description="慢线周期（须大于快线）")
    days: int = Field(500, ge=120, le=500, description="回测数据天数")

    @field_validator("symbol")
    @classmethod
    def _v(cls, v: str) -> str:
        v = str(v).strip().zfill(6)
        if not v.isdigit():
            raise ValueError(f"非法股票代码: {v}")
        return v

    @field_validator("slow")
    @classmethod
    def _slow(cls, v: int, info) -> int:
        fast = (info.data or {}).get("fast", 5)
        if v <= fast:
            raise ValueError(f"slow({v}) 必须 > fast({fast})")
        return v


class WalkForwardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6位A股代码")
    max_folds: int = Field(4, ge=1, le=6, description="滚动折数")

    @field_validator("symbol")
    @classmethod
    def _v(cls, v: str) -> str:
        v = str(v).strip().zfill(6)
        if not v.isdigit():
            raise ValueError(f"非法股票代码: {v}")
        return v


class FactorParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(..., min_length=5, max_length=20,
                               description="因子分析标的池（≥5 只，≤20 只）")
    days: int = Field(500, ge=250, le=500)

    @field_validator("symbols")
    @classmethod
    def _v(cls, v):
        out = [str(s).strip().zfill(6) for s in v]
        if any(not s.isdigit() for s in out):
            raise ValueError("非法股票代码")
        return out


class QuantTools:
    def __init__(self, market, bucket: TokenBucket | None = None):
        self.market = market
        self.bucket = bucket or TokenBucket()
        self._df_cache: dict[str, tuple] = {}

    async def _get_df(self, symbol: str, days: int):
        return await self.market.get_kline_raw(symbol, days, "hfq")

    async def run_backtest_tool(self, symbol: str, fast: int = 5, slow: int = 20,
                                days: int = 500) -> dict:
        df = await self._get_df(symbol, days)
        if len(df) < 60:
            return {"ok": False, "reason": f"K线数据不足: {len(df)} 行"}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: run_backtest(df, symbol, fast, slow))
        return result

    async def walk_forward_tool(self, symbol: str, max_folds: int = 4) -> dict:
        df = await self._get_df(symbol, 500)
        if len(df) < 630:  # 504 训练 + 126 样本外
            return {"ok": False,
                    "reason": f"数据不足切折: {len(df)} 行（需 ≥630，2年训练+6月样本外）"}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: walk_forward(df, symbol, max_folds))

    async def factor_analysis_tool(self, symbols: list[str], days: int = 500) -> dict:
        """标的池 → 价格宽表 + 基本面截面（演示口径）→ IC 表。"""
        frames = {}
        for sym in symbols:
            try:
                df = await self._get_df(sym, days)
                s = df.set_index("date")["close"].astype(float)
                frames[sym] = s
            except Exception as e:
                logger.warning("因子池标的 %s 数据失败: %s", sym, e)
        if len(frames) < 5:
            return {"ok": False, "reason": f"有效标的不足 5 只: {len(frames)}"}
        import pandas as pd  # noqa: PLC0415

        price_wide = pd.DataFrame(frames).dropna()
        if price_wide.shape[0] < 150:
            return {"ok": False, "reason": f"价格宽表过短: {price_wide.shape[0]} 行"}
        # 基本面截面（PE/ROE 从快照取；演示口径固定截面）
        snapshot = self.market.snapshot
        fund_rows = {}
        for sym in price_wide.columns:
            row = snapshot.row(sym) or {}
            fund_rows[sym] = {"pe": row.get("市盈率-动态"), "roe": row.get("市净率")}
        fundamentals = pd.DataFrame(fund_rows).T
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: factor_ic_table(price_wide, fundamentals))
