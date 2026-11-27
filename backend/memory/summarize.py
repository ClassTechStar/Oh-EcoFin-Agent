"""分层摘要（长会话成本 -70%）。

触发：prompt 估算 tokens > 20k 或轮次 > 20。
保留：最近 6 轮原文 + 两层摘要（事实层 verbatim / 结论层要点）。
深度优化：摘要后做事实层回读校验——正则抽取原会话所有 6 位股票代码与
百分比数字 → 摘要文本必须包含，缺一即补录（防"摘丢"关键事实）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SYMBOL_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
PCT_RE = re.compile(r"-?\d+(?:\.\d+)?%")
TOKENS_PER_CHAR = 0.6  # 中文粗估 tokens/字符（成本守门用，非精确计价）


def estimate_tokens(text: str) -> int:
    return int(len(text) * TOKENS_PER_CHAR)


@dataclass
class LayeredSummary:
    facts: list[str]
    conclusions: list[str]


def build_summary_prompt(conversation: str) -> str:
    from backend.engine.prompts import SUMMARY_PROMPT

    return SUMMARY_PROMPT.format(conversation=conversation[:20000])


def parse_summary_json(text: str) -> LayeredSummary:
    """容错解析摘要 JSON（模型可能包 ```json 栅栏）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|```\s*$", "", text, flags=re.MULTILINE)
    try:
        data = json.loads(text)
        return LayeredSummary(
            facts=[str(x) for x in data.get("facts", [])],
            conclusions=[str(x) for x in data.get("conclusions", [])],
        )
    except json.JSONDecodeError:
        return LayeredSummary(facts=[text[:500]], conclusions=[])


def fact_readback_verify(summary: LayeredSummary, conversation: str) -> LayeredSummary:
    """事实层回读校验：6 位股票代码与百分比必须保留，缺一补录。"""
    summary_text = " ".join(summary.facts)
    required_symbols = set(SYMBOL_RE.findall(conversation))
    required_pcts = set(PCT_RE.findall(conversation))
    missing = []
    for s in sorted(required_symbols):
        if s not in summary_text:
            missing.append(s)
    for p in sorted(required_pcts, key=len, reverse=True):
        if p not in summary_text:
            missing.append(f"{p}(百分比)")
    if missing:
        summary.facts.append("原会话关键事实补录: " + ", ".join(missing))
    return summary


def render_history(summary: LayeredSummary, recent_rounds: list[dict]) -> list[dict]:
    """渲染为 messages：system 级摘要块 + 最近 6 轮原文。"""
    msgs: list[dict] = []
    if summary.facts or summary.conclusions:
        block = {"facts": summary.facts, "conclusions": summary.conclusions}
        msgs.append({"role": "system", "content": "【会话摘要】\n" + json.dumps(
            block, ensure_ascii=False)})
    for m in recent_rounds:
        msgs.append({"role": m["role"], "content": m["content"]})
    return msgs


def should_summarize(n_tokens: int, n_rounds: int, *, token_trigger: int = 20000,
                     round_trigger: int = 20) -> bool:
    return n_tokens > token_trigger or n_rounds > round_trigger


def keep_recent(conversation: list[dict], keep_rounds: int = 6) -> tuple[str, list[dict]]:
    """切分：旧部分进摘要、最近 keep_rounds*2 条保留原文。返回 (旧文本, 保留列表)。"""
    keep_n = keep_rounds * 2
    if len(conversation) <= keep_n:
        return "", conversation
    old = conversation[:-keep_n]
    recent = conversation[-keep_n:]
    old_text = "\n".join(f"{m['role']}: {m['content']}" for m in old)
    return old_text, recent
