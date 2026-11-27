"""智能投顾工具：对话式风险测评题库 / 计分 / Markowitz 优化 + 合规审查链。"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.quant.compliance import Portfolio, compliance_check, is_equity, map_score_to_level

# ---------- 题库（5 题，维度制；每题选项带 1-5 分） ----------

RISK_QUESTIONS = [
    {
        "qid": "q1", "dimension": "投资经验",
        "question": "您参与股票/基金投资多久了？",
        "options": [
            {"text": "没投资过，第一次接触", "score": 1},
            {"text": "1 年以内", "score": 2},
            {"text": "1-3 年", "score": 3},
            {"text": "3-5 年", "score": 4},
            {"text": "5 年以上", "score": 5},
        ],
    },
    {
        "qid": "q2", "dimension": "收益目标",
        "question": "您更接受哪种一年期结果？",
        "options": [
            {"text": "稳稳跑赢存款就行（0-3%）", "score": 1},
            {"text": "略高于通胀（3-6%）", "score": 2},
            {"text": "接近市场平均（6-10%）", "score": 3},
            {"text": "追求较高收益，能接受波动（10-15%）", "score": 4},
            {"text": "追求高收益，接受大幅波动（15%+）", "score": 5},
        ],
    },
    {
        "qid": "q3", "dimension": "亏损容忍",
        "question": "账户短期回撤 20%，您会？",
        "options": [
            {"text": "无法接受，立即清仓不再投资", "score": 1},
            {"text": "卖出大部分，只留少量观察", "score": 2},
            {"text": "先观察一段时间再决定", "score": 3},
            {"text": "按计划继续持有", "score": 4},
            {"text": "反而考虑逢低加仓", "score": 5},
        ],
    },
    {
        "qid": "q4", "dimension": "投资期限",
        "question": "这笔钱预计多久不用？",
        "options": [
            {"text": "1 年以内", "score": 1},
            {"text": "1-3 年", "score": 3},
            {"text": "3 年以上", "score": 5},
        ],
    },
    {
        "qid": "q5", "dimension": "财务状况",
        "question": "应急资金可覆盖几个月支出？",
        "options": [
            {"text": "没有应急资金", "score": 1},
            {"text": "3 个月以内", "score": 2},
            {"text": "3-6 个月", "score": 3},
            {"text": "6-12 个月", "score": 4},
            {"text": "12 个月以上", "score": 5},
        ],
    },
]

DISCLAIMER_TEXT = (
    "本组合由教学演示智能体生成，仅供课程学习使用，不构成任何投资建议；"
    "数据可能存在延迟或误差，请以交易所官方信息为准。"
)

#: 五档风险画像（报告 1.8：保守/稳健/平衡/成长/进取）
PROFILES = {
    "R1": {"label": "保守", "equity_cap": 0.0, "single_cap": 0.0,
           "drawdown": "≤5%", "horizon": "1 年以内", "benchmark": "货币基金/存款"},
    "R2": {"label": "稳健", "equity_cap": 0.10, "single_cap": 0.10,
           "drawdown": "≤10%", "horizon": "1-3 年", "benchmark": "纯债/固收+"},
    "R3": {"label": "平衡", "equity_cap": 0.35, "single_cap": 0.20,
           "drawdown": "≤15%", "horizon": "3 年左右", "benchmark": "股债平衡"},
    "R4": {"label": "成长", "equity_cap": 0.45, "single_cap": 0.25,
           "drawdown": "≤25%", "horizon": "3-5 年", "benchmark": "沪深 300"},
    "R5": {"label": "进取", "equity_cap": 0.50, "single_cap": 0.30,
           "drawdown": ">25%", "horizon": "5 年以上", "benchmark": "偏股指数"},
}


def profile_for_level(level: str) -> dict:
    profile = PROFILES.get(level)
    if profile is None:
        raise ValueError(f"未知风险等级: {level}")
    return {"level": level, **profile}



def score_answer(question: dict, choice_text: str) -> int:
    """按选项文本计分（话术可变、评分口径不可变——按选项序号兜底）。"""
    for opt in question["options"]:
        if opt["text"] == choice_text:
            return opt["score"]
    # 上下文感知改写后的措辞 → 按序号匹配（前端点选返回文本，需带序号映射）
    for i, opt in enumerate(question["options"], 1):
        if choice_text.endswith(f"（{i}）") or choice_text.endswith(f"({i})"):
            return opt["score"]
    raise ValueError(f"答案与题库选项不匹配: {choice_text}")


def score_answers(answers: list[dict]) -> tuple[int, str]:
    """answers: [{qid, choice}] → (总分, level)。"""
    by_qid = {q["qid"]: q for q in RISK_QUESTIONS}
    total = 0
    for a in answers:
        q = by_qid.get(a["qid"])
        if q is None:
            raise ValueError(f"未知题目: {a['qid']}")
        total += score_answer(q, str(a["choice"]))
    return total, map_score_to_level(total)


# ---------- 工具参数模型 ----------

class PortfolioParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(..., min_length=3, max_length=10,
                               description="候选标的池（6位代码），3-10 只")
    level: str = Field(..., description="风险等级 R1-R5（由测评得出）")

    @field_validator("symbols")
    @classmethod
    def _v(cls, v):
        out = [str(s).strip().zfill(6) for s in v]
        if any(not s.isdigit() for s in out):
            raise ValueError("非法股票代码")
        return out

    @field_validator("level")
    @classmethod
    def _lv(cls, v):
        if v not in {"R1", "R2", "R3", "R4", "R5"}:
            raise ValueError("level 必须是 R1-R5")
        return v


class AskRiskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., description="向用户提出的问题（上下文感知措辞）")
    options: list[str] = Field(..., min_length=2, max_length=6,
                               description="2-6 个点选项")
    context: dict = Field(default_factory=dict, description="补充上下文")


class AdvisorTools:
    def __init__(self, market, optimizer):
        self.market = market
        self.optimizer = optimizer

    async def build_price_matrix(self, symbols: list[str], days: int = 500) -> pd.DataFrame:
        """标的池收盘价宽表（PyPortfolioOpt 吃价格 DataFrame）。"""
        frames = {}
        for sym in symbols:
            df = await self.market.get_kline_raw(sym, days)
            s = df.set_index("date")["close"].astype(float)
            frames[sym] = s
        prices = pd.DataFrame(frames).dropna()
        if prices.shape[0] < 60:
            raise ValueError(f"价格矩阵过短: {prices.shape[0]} 行")
        return prices

    async def portfolio_optimize(self, symbols: list[str], level: str) -> dict:
        """优化 + 合规审查链（确定性代码）+ 前沿缓存。

        权益上限语义：标的池全为权益时，权益权重按 equity_cap 等比缩放，
        余量自动分配为国债兜底仓（511010），剩余权重经合规链复核。
        """
        caps = {"R1": 0.0, "R2": 0.10, "R3": 0.35, "R4": 0.45, "R5": 0.50}
        singles = {"R1": 0.0, "R2": 0.10, "R3": 0.20, "R4": 0.25, "R5": 0.30}
        if level == "R1":
            return {"ok": False, "reason": "R1 等级不配置股票（权益上限 0%）",
                    "hint": "建议货币基金/存款类资产（演示不涉及）"}
        prices = await self.build_price_matrix(symbols)
        frontier = self.optimizer.efficient_frontier(
            prices, level, caps[level], singles[level])

        point = dict(frontier["point"]["weights"])
        equity_sum = sum(w for s, w in point.items() if is_equity(s))
        defensive: set[str] = set()
        if equity_sum > caps[level] + 1e-9:
            scale = caps[level] / equity_sum
            point = {s: round(w * scale, 6) for s, w in point.items() if is_equity(s)}
            bond_w = round(1.0 - sum(point.values()), 6)
            if bond_w > 1e-6:
                point["511010"] = bond_w
                defensive.add("511010")
        portfolio = Portfolio(weights=point, disclaimer=DISCLAIMER_TEXT,
                              defensive=defensive)
        violations = compliance_check(portfolio, level)
        return {
            "ok": not violations,
            "level": level,
            "weights": point,
            "point": {**frontier["point"], "weights": point},
            "frontier": frontier["frontier"],
            "tickers": frontier["tickers"],
            "defensive_sleeve": sorted(defensive),
            "compliance": [f"{v.rule}: {v.detail}" for v in violations],
            "disclaimer": DISCLAIMER_TEXT,
        }
