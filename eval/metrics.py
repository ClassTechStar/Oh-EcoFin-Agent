"""指标定义（14.3）：任务完成率 / 数字准确率 / 工具调用准确率 / 平均轮次与 token 成本。

数字准确率（幻觉率度量核心）：报告数字与工具原始返回比对
（精确值匹配 + 派生值容差 ±5%）→ 溯源命中率 = 被支持数字 / 全部数字。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

NUM_RE = re.compile(r"(?<![\w.\-])-?\d+(?:\.\d+)?(?:%|元|亿|万)?(?![\w.\-])")
CITE_RE = re.compile(r"\[(\d+)\]")


@dataclass
class TaskResult:
    task_id: str
    group: str
    config: str              # A/B/C/D/E
    repeat: int
    ok: bool = False
    rubric_score: float = 0.0        # LLM-judge 打分 0-1
    rubric_pass: bool = False        # ≥ 阈值
    numeric_support_rate: float = 0.0  # 数字溯源命中率
    tool_recall: float = 0.0         # 期望工具召回率
    tool_missing: list = field(default_factory=list)
    tool_extra: list = field(default_factory=list)
    rounds: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cents: int = 0
    fabricated_sample: str = ""      # C 组"编造数据"定性样例


def extract_numbers(text: str) -> list[float]:
    """从报告正文提取全部数字（去引用编号）。"""
    cleaned = CITE_RE.sub("", text)
    out = []
    for m in NUM_RE.finditer(cleaned):
        s = m.group(0).rstrip("%元亿万")
        try:
            out.append(float(s))
        except ValueError:
            continue
    return out


def collect_tool_returns(raw_tool_results: list[dict]) -> list[float]:
    """从工具原始返回递归收集全部数值（比对语料）。"""
    vals: list[float] = []
    for r in raw_tool_results or []:
        _walk_numbers(r.get("result", r), vals)
    return vals


def _walk_numbers(obj, out: list) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        for m in NUM_RE.finditer(obj):
            try:
                out.append(float(m.group(0).rstrip("%元亿万")))
            except ValueError:
                continue
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_numbers(v, out)


def numeric_support_rate(report_text: str, raw_tool_results: list[dict],
                         tolerance: float = 0.05) -> tuple[float, int, int]:
    """溯源命中率 = 被支持数字 / 全部数字（精确匹配 + 派生容差 ±5%）。"""
    nums = extract_numbers(report_text)
    if not nums:
        return 1.0, 0, 0
    corpus = collect_tool_returns(raw_tool_results)
    corpus_set = set(corpus)
    supported = 0
    for n in nums:
        if n in corpus_set:
            supported += 1
            continue
        for c in corpus:
            if c != 0 and abs(abs(n) - abs(c)) / max(abs(c), 1e-9) <= tolerance:
                supported += 1
                break
    return supported / len(nums), supported, len(nums)


def tool_recall(called: list[str], expected: list[str]) -> tuple[float, list, list]:
    """期望工具集合的召回率（缺调/错调分别计）。"""
    called_set = set(called)
    expected_set = set(expected)
    if not expected_set:
        return 1.0, [], sorted(called_set)
    missing = sorted(expected_set - called_set)
    extra = sorted(called_set - expected_set)
    recall = len(expected_set & called_set) / len(expected_set)
    return recall, missing, extra


def aggregate(results: list[TaskResult]) -> dict:
    """按 config 聚合：均值 ± 标准差（重复 3 次 seed 固定）。"""
    import statistics

    by_config: dict[str, list[TaskResult]] = {}
    for r in results:
        by_config.setdefault(r.config, []).append(r)
    table = {}
    for cfg, rs in by_config.items():
        def mean_std(vals):
            if not vals:
                return (0.0, 0.0)
            m = statistics.mean(vals)
            s = statistics.stdev(vals) if len(vals) > 1 else 0.0
            return (round(m, 4), round(s, 4))
        completion = [1.0 if r.rubric_pass else 0.0 for r in rs]
        table[cfg] = {
            "n": len(rs),
            "completion_rate": mean_std(completion),
            "numeric_support_rate": mean_std([r.numeric_support_rate for r in rs]),
            "tool_recall": mean_std([r.tool_recall for r in rs]),
            "avg_rounds": mean_std([float(r.rounds) for r in rs]),
            "avg_tokens_in": mean_std([float(r.tokens_in) for r in rs]),
            "avg_tokens_out": mean_std([float(r.tokens_out) for r in rs]),
            "total_cost_cents": sum(r.cost_cents for r in rs),
        }
    return table


def render_markdown(table: dict) -> str:
    """消融结果 Markdown 表（报告用）。"""
    cols = ["config", "完成率", "数字溯源命中率", "工具召回率", "平均轮次",
            "平均输入tokens", "平均输出tokens", "成本(分)"]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "---|" * len(cols)]
    for cfg, row in table.items():
        def fmt(v):
            if isinstance(v, tuple):
                return f"{v[0]:.3f}±{v[1]:.3f}"
            return str(v)
        lines.append("| " + " | ".join([
            cfg, fmt(row["completion_rate"]), fmt(row["numeric_support_rate"]),
            fmt(row["tool_recall"]), fmt(row["avg_rounds"]),
            fmt(row["avg_tokens_in"]), fmt(row["avg_tokens_out"]),
            fmt(row["total_cost_cents"])]) + " |")
    return "\n".join(lines)


def load_jsonl(path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
