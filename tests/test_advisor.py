"""投顾工具：题库计分（话术可变、评分口径不变）/ portfolio_optimize 全链。"""

from __future__ import annotations

import pytest

from backend.quant.portfolio import PortfolioOptimizer
from backend.tools.advisor import (
    DISCLAIMER_TEXT,
    RISK_QUESTIONS,
    AskRiskParams,
    PortfolioParams,
    score_answer,
    score_answers,
)
from backend.tools.market import MarketTools


def test_question_bank_dimensions():
    assert len(RISK_QUESTIONS) == 5
    dims = [q["dimension"] for q in RISK_QUESTIONS]
    assert dims == ["投资经验", "收益目标", "亏损容忍", "投资期限", "财务状况"]
    for q in RISK_QUESTIONS:
        assert len(q["options"]) >= 2
        scores = [o["score"] for o in q["options"]]
        assert scores == sorted(scores)  # 选项分数单调递增


def test_score_answer_exact_match():
    q = RISK_QUESTIONS[0]
    assert score_answer(q, "没投资过，第一次接触") == 1
    assert score_answer(q, "5 年以上") == 5


def test_score_answer_adapted_wording_by_index():
    """上下文感知追问：措辞改写后按选项序号兜底计分（话术可变、评分口径不变）。"""
    q = RISK_QUESTIONS[0]
    adapted = "您第一次接触投资是什么时候？（1）刚接触（2）…（5）5 年以上"
    assert score_answer(q, "刚接触（1）") == 1
    assert score_answer(q, "5 年以上（5）") == 5


def test_score_answers_r1_to_r5():
    r1 = [{"qid": "q1", "choice": "没投资过，第一次接触"},
          {"qid": "q2", "choice": "稳稳跑赢存款就行（0-3%）"},
          {"qid": "q3", "choice": "无法接受，立即清仓不再投资"},
          {"qid": "q4", "choice": "1 年以内"},
          {"qid": "q5", "choice": "没有应急资金"}]
    total, level = score_answers(r1)
    assert total == 5 and level == "R1"

    r5 = [{"qid": "q1", "choice": "5 年以上"},
          {"qid": "q2", "choice": "追求高收益，接受大幅波动（15%+）"},
          {"qid": "q3", "choice": "反而考虑逢低加仓"},
          {"qid": "q4", "choice": "3 年以上"},
          {"qid": "q5", "choice": "6-12 个月"}]
    total5, level5 = score_answers(r5)
    assert total5 == 24 and level5 == "R5"


def test_score_answers_rejects_unknown_choice():
    with pytest.raises(ValueError):
        score_answers([{"qid": "q1", "choice": "瞎答的"}])
    with pytest.raises(ValueError):
        score_answers([{"qid": "q99", "choice": "x"}])


def test_repeat_dialogue_same_level_different_wording():
    """验收：同一用户重复对话 → 追问措辞不同而等级判定一致。

    同一分值答案，一次原文、一次措辞自适应（选项序号兜底）→ 等级不变。
    """
    def answers(adapted_q1: bool):
        q1 = "5 年以上（5）" if adapted_q1 else "5 年以上"
        q3 = "按计划继续持有（4）" if adapted_q1 else "按计划继续持有"
        return [{"qid": "q1", "choice": q1},
                {"qid": "q2", "choice": "接近市场平均（6-10%）"},
                {"qid": "q3", "choice": q3},
                {"qid": "q4", "choice": "3 年以上"},
                {"qid": "q5", "choice": "6-12 个月"}]

    _, level_a = score_answers(answers(False))
    _, level_b = score_answers(answers(True))
    assert level_a == level_b == "R4"


def test_params_models_validation():
    p = PortfolioParams(symbols=["600519", "000001", "510300"], level="R3")
    assert len(p.symbols) == 3
    with pytest.raises(Exception):
        PortfolioParams(symbols=["600519"], level="R3")  # min 3
    with pytest.raises(Exception):
        PortfolioParams(symbols=["600519", "000001", "510300"], level="R9")
    with pytest.raises(Exception):
        AskRiskParams(question="q", options=["only-one"])


def make_fake_market(tmp_path, symbols):
    """合成 K 线卡带市场（供 portfolio_optimize 数据矩阵；确定性种子）。"""
    import json
    import zlib

    from tests.helpers import synthetic_ohlcv

    cass = tmp_path / "cassette"
    (cass / "klines").mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        seed = zlib.crc32(sym.encode()) % 1000
        df = synthetic_ohlcv(500, seed=seed)
        (cass / "klines" / f"{sym}.json").write_text(
            json.dumps(df.to_dict(orient="records"), default=str), encoding="utf-8")
    snap = type("SN", (), {"row": lambda self, s: None, "as_of": "t"})()
    from backend.data.ratelimit import TokenBucket

    return MarketTools(snapshot=snap, bucket=TokenBucket(100, 10),
                       kline_cache_ttl=0, cassette_dir=str(cass), demo_mode=True)


async def test_portfolio_optimize_full_chain(tmp_path):
    """优化 + 合规审查链 + 前沿数据（R3 合规通过）。"""
    symbols = ["600519", "000001", "601318", "300750", "510300"]
    market = make_fake_market(tmp_path, symbols)
    from backend.tools.advisor import AdvisorTools

    at = AdvisorTools(market, PortfolioOptimizer())
    out = await at.portfolio_optimize(symbols, "R3")
    assert out["ok"] is True
    assert out["level"] == "R3"
    assert out["compliance"] == []  # 合规链通过（空列表）
    assert sum(out["weights"].values()) == pytest.approx(1.0, abs=0.02)
    assert len(out["frontier"]) >= 10
    assert out["disclaimer"] == DISCLAIMER_TEXT


async def test_portfolio_optimize_r1_rejected(tmp_path):
    """R1 等级不配置股票（权益上限 0%）。"""
    market = make_fake_market(tmp_path, ["600519", "000001", "601318"])
    from backend.tools.advisor import AdvisorTools

    at = AdvisorTools(market, PortfolioOptimizer())
    out = await at.portfolio_optimize(["600519", "000001", "601318"], "R1")
    assert out["ok"] is False and "R1" in out["reason"]
