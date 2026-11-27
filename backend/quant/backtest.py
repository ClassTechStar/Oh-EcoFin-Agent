"""Backtrader A 股规则回测（完整建模四规则 + 后复权 + 防未来函数）。

规则建模：
| 规则 | 实现 |
|---|---|
| 佣金 万2.5 最低5元 | AShareCommission._getcommission max(fee, 5) |
| 印花税 0.05% 仅卖出 | size<0 分支加 0.0005×成交额 |
| T+1 | 策略层持仓账本：仅允许卖出"昨日及更早"买入的持仓 |
| 涨跌停 | 开盘较昨收触及档位阈值 → 跳过当日下单 |

防未来函数铁律：信号 t 收盘计算、t+1 开盘成交（cheat_on_open=False）。
AKShare 中文列名重映射在 tools/market.get_kline_raw 已归一，此处校验英文列。
backtrader 全部函数内延迟导入（冷启动 <2s 纪律）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
STAMP_DUTY = 0.0005
INIT_CASH = 1_000_000


def validate_columns(df: pd.DataFrame) -> None:
    """AKShare→Backtrader 适配前置校验（最常见的翻车点）。"""
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"K线缺列（AKShare 列名漂移？）: {missing}；"
                         "请检查 tools/market.get_kline_raw 的重映射层")


def limit_pct_for(symbol: str, name: str = "") -> float:
    """涨跌停档位（与 data.quality 同口径）。"""
    if "ST" in name.upper():
        return 5.0
    if str(symbol).startswith(("30", "68")):
        return 20.0
    return 10.0


def build_cerebro(df: pd.DataFrame, symbol: str, strategy_cls, strategy_params: dict,
                  cash: float = INIT_CASH):
    """装配 Cerebro：数据适配 + A股佣金 scheme + 策略。"""
    import backtrader as bt

    validate_columns(df)
    data = df.set_index("date")
    data.index = pd.to_datetime(data.index)

    class AKShareData(bt.feeds.PandasData):
        params = (
            ("datetime", None), ("open", "open"), ("high", "high"),
            ("low", "low"), ("close", "close"), ("volume", "volume"),
            ("openinterest", -1),
        )

    class AShareCommission(bt.CommInfoBase):
        params = (
            ("commission", COMMISSION_RATE), ("stocklike", True),
            ("commtype", bt.CommInfoBase.COMM_PERC),
            ("stamp_duty", STAMP_DUTY), ("min_commission", MIN_COMMISSION),
        )

        def _getcommission(self, size, price, pseudoexec):
            turnover = abs(size) * price
            fee = max(turnover * self.p.commission, self.p.min_commission)
            if size < 0:  # 印花税只在卖侧
                fee += turnover * self.p.stamp_duty
            return fee

    cerebro = bt.Cerebro(cheat_on_open=False)  # 防未来函数铁律
    cerebro.adddata(AKShareData(dataname=data), name=str(symbol))
    cerebro.broker.setcash(cash)
    cerebro.broker.addcommissioninfo(AShareCommission())
    cerebro.addstrategy(strategy_cls, **strategy_params)
    return cerebro


class DualMAStrategy:
    """双均线策略（全进全出）：T+1 账本 + 涨跌停跳过。工厂方法延迟导入 Backtrader。"""

    PARAM_GRID_FAST = (5, 10, 20)
    PARAM_GRID_SLOW = (20, 60, 120)

    @staticmethod
    def build(symbol: str, fast: int = 5, slow: int = 20):
        import backtrader as bt

        class _DualMA(bt.Strategy):
            params = (("fast", fast), ("slow", slow), ("symbol", symbol),
                      ("pct_limit", None))

            def __init__(self):
                ema_fast = bt.ind.EMA(self.data.close, period=self.p.fast)
                ema_slow = bt.ind.EMA(self.data.close, period=self.p.slow)
                self.crossover = ema_fast - ema_slow  # 信号 t 收盘计算
                self._buy_dates: list = []   # T+1 持仓账本
                self._entry_price = 0.0

            def next(self):
                # 涨跌停：开盘较昨收触及档位 → 跳过当日下单（成交假设不成立）
                if self.p.pct_limit and len(self.data) >= 2:
                    prev_close = self.data.close[-1]
                    open_today = self.data.open[0]
                    if prev_close:
                        chg = abs(open_today / prev_close - 1) * 100
                        if chg >= self.p.pct_limit * 0.99:
                            return
                today = self.data.datetime.date(0)
                pos = self.position
                if not pos:
                    if self.crossover[0] > 0:
                        self.buy(size=self._allin_size())
                        self._entry_price = float(self.data.open[0])
                        self._buy_dates.append(today)
                else:
                    # T+1：仅允许卖出"昨日及更早"买入的持仓
                    can_sell = any(d < today for d in self._buy_dates)
                    if can_sell and self.crossover[0] < 0:
                        self.close()
                        self._buy_dates = []

            def _allin_size(self) -> int:
                cash = self.broker.getcash()
                price = float(self.data.open[0])
                size = int(cash * 0.995 / price / 100) * 100  # A股一手 100 股
                return max(size, 100)

        return _DualMA

    @staticmethod
    def grid() -> list[dict]:
        """fast ∈ {5,10,20} × slow ∈ {20,60,120}，fast < slow 约束。"""
        out = []
        for f in DualMAStrategy.PARAM_GRID_FAST:
            for s in DualMAStrategy.PARAM_GRID_SLOW:
                if f < s:
                    out.append({"fast": f, "slow": s})
        return out


@dataclass
class BacktestMetrics:
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_loss_ratio: float
    trade_count: int


def metrics_from_returns(returns: pd.Series, trade_pnls: list[float]) -> BacktestMetrics:
    """从日收益率序列与交易盈亏提取指标表（纯函数，好单测）。"""
    r = pd.Series(returns).dropna().astype(float)
    total_return = float((1 + r).prod() - 1) if len(r) else 0.0
    annual = float(r.mean() * 244) if len(r) else 0.0
    sharpe = float(r.mean() / r.std() * (244 ** 0.5)) if len(r) > 1 and r.std() else 0.0
    equity = (1 + r).cumprod()
    mdd = float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    win_rate = len(wins) / len(trade_pnls) if trade_pnls else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    plr = avg_win / avg_loss if avg_loss else 0.0
    return BacktestMetrics(
        total_return=round(total_return, 4), annual_return=round(annual, 4),
        sharpe=round(sharpe, 3), max_drawdown=round(mdd, 4),
        win_rate=round(win_rate, 4), profit_loss_ratio=round(plr, 3),
        trade_count=len(trade_pnls),
    )


def run_backtest(df: pd.DataFrame, symbol: str, fast: int = 5, slow: int = 20,
                 cash: float = INIT_CASH) -> dict:
    """双均线回测主入口（同步函数，调用方在线程池执行）。"""
    import backtrader as bt

    strategy_cls = DualMAStrategy.build(symbol, fast, slow)
    cerebro = build_cerebro(df, symbol, strategy_cls,
                            {"pct_limit": limit_pct_for(symbol)}, cash)
    trade_pnls: list[float] = []

    class _TradeRecorder(bt.Analyzer):
        def notify_trade(self, trade):
            if trade.isclosed:
                trade_pnls.append(trade.pnlcomm)

    cerebro.addanalyzer(_TradeRecorder, _name="recorder")
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="timereturn",
                        timeframe=bt.TimeFrame.Days)
    result = cerebro.run()[0]
    analysis = result.analyzers.timereturn.get_analysis()
    returns = pd.Series(analysis).astype(float)
    m = metrics_from_returns(returns, trade_pnls)
    return {"ok": True, "symbol": symbol, "params": {"fast": fast, "slow": slow},
            "metrics": m.__dict__, "final_value": cerebro.broker.getvalue()}


def grid_search(df: pd.DataFrame, symbol: str,
                grid: list[dict] | None = None) -> tuple[dict, float, list[dict]]:
    """样本内参数寻优（全参数网格）。返回 (best_params, best_sharpe, 全部结果)。"""
    grid = grid or DualMAStrategy.grid()
    results = []
    for params in grid:
        try:
            r = run_backtest(df, symbol, **params)
            results.append({**params, "sharpe": r["metrics"]["sharpe"],
                            "total_return": r["metrics"]["total_return"]})
        except Exception as e:
            logger.warning("网格参数 %s 回测失败: %s", params, e)
    if not results:
        raise RuntimeError("全网格回测失败")
    best = max(results, key=lambda x: x["sharpe"])
    return {"fast": best["fast"], "slow": best["slow"]}, best["sharpe"], results
