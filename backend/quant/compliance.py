"""合规审查链（适当性管理逻辑的确定性工程化）。

合规规则的执行不依赖模型自觉——审查是确定性代码而非 LLM 判断
（LLM 只解释结果）。空列表 = 通过；非空返回给引擎要求修正并复检。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 风险等级 → 权益类上限 / 单一资产上限（与 config.advisor 一致）
EQUITY_CAPS = {"R1": 0.0, "R2": 0.10, "R3": 0.35, "R4": 0.45, "R5": 0.50}
SINGLE_CAPS = {"R1": 0.0, "R2": 0.10, "R3": 0.20, "R4": 0.25, "R5": 0.30}

#: 行业集中度告警阈值（参考 multi-agent-robo-advisor 合规四项）
INDUSTRY_CAP = 0.33

#: 收益承诺敏感表述（合规审查只做确定性词表，不依赖模型自觉）
PROMISE_PHRASES = (
    "保证收益", "保本保息", "稳赚不赔", "稳赚", "无风险", "必涨", "必赚",
    "承诺收益", "确定收益", "包赚", "零风险", "绝对收益",
)

#: 教学演示统一免责声明
ADVICE_DISCLAIMER = (
    "本组合由教学演示智能体生成，仅供课程学习使用，不构成任何投资建议；"
    "数据可能存在延迟或误差，请以交易所官方信息为准。"
)

# 权益类标的识别（A 股：沪深主板/创业板/科创板均为股票；基金/债券/现金类不在其列）
EQUITY_PREFIXES = ("6", "0", "3")


@dataclass
class Violation:
    rule: str
    detail: str
    severity: str = "block"  # block | warn


@dataclass
class Portfolio:
    weights: dict[str, float]          # symbol → weight
    disclaimer: str = ""               # 免责声明文本（结构上强制存在）
    asset_types: dict[str, str] = field(default_factory=dict)  # symbol → stock|fund|bond|cash
    defensive: set[str] = field(default_factory=set)  # 债券/现金兜底仓（免单一上限）


def is_equity(symbol: str, asset_types: dict[str, str] | None = None) -> bool:
    if asset_types and symbol in asset_types:
        return asset_types[symbol] == "stock"
    if str(symbol).startswith(("511", "019", "518", "CASH", "BOND")):
        return False  # 国债/黄金/现金类兜底
    return str(symbol).startswith(EQUITY_PREFIXES)


def compliance_check(portfolio: Portfolio, level: str) -> list[Violation]:
    """确定性合规链：占比超限 / 单一超限 / 缺声明 三规则。"""
    v: list[Violation] = []
    caps = EQUITY_CAPS.get(level)
    singles = SINGLE_CAPS.get(level)
    if caps is None or singles is None:
        v.append(Violation("risk_level", f"未知风险等级: {level}"))
        return v

    equity_sum = sum(w for s, w in portfolio.weights.items()
                     if is_equity(s, portfolio.asset_types))
    if equity_sum > caps + 1e-9:
        v.append(Violation(
            "equity_cap",
            f"股票占比 {equity_sum:.1%} 超风险等级 {level} 上限 {caps:.0%}"))

    if portfolio.weights:
        risky = {s: w for s, w in portfolio.weights.items() if s not in portfolio.defensive}
        if risky:
            sym, w = max(risky.items(), key=lambda kv: kv[1])
            if w > singles + 1e-9:
                v.append(Violation(
                    "single_cap",
                    f"单一资产 {sym} 占比 {w:.1%} 超 {level} 上限 {singles:.0%}"))

    if not portfolio.disclaimer or len(portfolio.disclaimer) < 20:
        v.append(Violation(
            "disclaimer", "组合结果缺少免责声明（防御性检查，结构上不可能缺失）"))
    return v


def map_score_to_level(total_score: int) -> str:
    """测评总分 → R1-R5 五级映射。

    计分口径：5 题 × 每题 1-5 分，总分范围 5-25。
    R1: 5-8（保守）R2: 9-12 R3: 13-17 R4: 18-21 R5: 22-25（积极）。
    话术可变、评分口径不可变（合规红线）。
    """
    if total_score < 9:
        return "R1"
    if total_score < 13:
        return "R2"
    if total_score < 18:
        return "R3"
    if total_score < 22:
        return "R4"
    return "R5"


def check_industry_concentration(weights: dict[str, float],
                                 industry: dict[str, str] | None,
                                 cap: float = INDUSTRY_CAP) -> list[Violation]:
    """行业集中度：任一行权重和 > cap 报警（缺映射则跳过，不猜行业）。"""
    if not industry:
        return []
    exposure: dict[str, float] = {}
    for symbol, weight in weights.items():
        group = industry.get(symbol)
        if group:
            exposure[group] = exposure.get(group, 0.0) + abs(float(weight))
    return [
        Violation("industry_concentration",
                  f"行业 {group} 权重 {value:.1%} 超集中度上限 {cap:.0%}", "warn")
        for group, value in sorted(exposure.items()) if value > cap + 1e-9
    ]


def check_return_promise(text: str) -> list[Violation]:
    """收益表述审查：命中承诺类词表即告警（不做语义推断，可解释可复核）。"""
    if not text:
        return []
    hits = [p for p in PROMISE_PHRASES if p in text]
    if not hits:
        return []
    return [Violation("return_promise",
                      f"检测到收益承诺类表述：{', '.join(hits)}（禁止承诺收益）",
                      "block")]


def review_advice(weights: dict[str, float], level: str, *,
                  industry: dict[str, str] | None = None,
                  statement: str = "",
                  disclaimer: str = ADVICE_DISCLAIMER) -> dict:
    """合规四项汇总：适当性/集中度/收益表述/免责声明 → status + 明细。"""
    defensive = {s for s in weights if str(s).startswith(("511", "019", "518"))
                 or str(s).upper() in ("CASH", "BOND")}
    portfolio = Portfolio(weights=weights, disclaimer=disclaimer,
                          defensive=defensive)
    violations = compliance_check(portfolio, level)
    violations += check_industry_concentration(weights, industry)
    violations += check_return_promise(statement)
    return {
        "status": "attention" if violations else "passed",
        "violations": [{"rule": v.rule, "detail": v.detail,
                        "severity": v.severity} for v in violations],
        "disclaimer": disclaimer,
    }

