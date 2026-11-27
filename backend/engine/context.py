"""上下文预算与压缩（M6）。

两级策略：
1. 压缩工具结果：保留状态/摘要/哈希等关键字段，表格只留少量行；
2. 丢弃最老的完整对话单元（以 user 消息为界，保持 assistant tool_calls
   与 tool 响应的配对），最后注入一条预算提示。

压缩后仍超预算 → 由编排层收尾（done.stop_reason=context_budget），不再调用模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from backend.memory.summarize import TOKENS_PER_CHAR, estimate_tokens

_KEEP_KEYS = (
    "ok", "kind", "summary", "error", "hint", "result_hash", "sql_hash",
    "chart_spec_hash", "row_count", "truncated", "data_as_of",
    "requires_confirmation", "side_effect", "report_id", "spec_hash",
)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif content is not None:
            total += estimate_tokens(json.dumps(content, ensure_ascii=False, default=str))
        if message.get("tool_calls"):
            total += estimate_tokens(
                json.dumps(message["tool_calls"], ensure_ascii=False, default=str))
        total += 8  # message overhead
    return total


def _truncate(text: str, token_limit: int) -> str:
    max_chars = max(120, int(token_limit / max(TOKENS_PER_CHAR, 0.01)))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…[已按上下文预算截断]"


def _compact_tool_content(content: object, token_limit: int) -> str:
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return _truncate(content, token_limit)
    else:
        payload = content
    if not isinstance(payload, dict):
        return _truncate(str(payload), token_limit)

    compact = {key: payload[key] for key in _KEEP_KEYS if key in payload}
    table = payload.get("table")
    if isinstance(table, dict):
        rows = table.get("rows") or []
        compact["table"] = {
            "columns": table.get("columns") or [],
            "rows": rows[:3],
            "truncated": len(rows) > 3,
        }
    items = payload.get("items")
    if isinstance(items, list):
        compact["items"] = items[:3]
    if "datasets" in payload and isinstance(payload["datasets"], list):
        compact["datasets"] = payload["datasets"][:5]
    compact["budget_note"] = "工具结果已按上下文预算压缩；完整结果见 tool_calls/数据集"
    return _truncate(json.dumps(compact, ensure_ascii=False, default=str), token_limit)


def _split_units(messages: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    prefix: list[dict] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        prefix.append(messages[index])
        index += 1
    units: list[list[dict]] = []
    current: list[dict] = []
    for message in messages[index:]:
        if message.get("role") == "user" and current:
            units.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        units.append(current)
    return prefix, units


@dataclass
class ContextFit:
    messages: list[dict]
    used_before: int
    used_after: int
    compressed: bool = False
    over_budget: bool = False
    tool_compressed: int = 0
    dropped_units: int = 0


def fit_context(messages: list[dict], *, context_tokens: int,
                tool_result_tokens: int, keep_recent_messages: int) -> ContextFit:
    used_before = estimate_messages_tokens(messages)
    if used_before <= context_tokens:
        return ContextFit(messages, used_before, used_before)

    fitted = [dict(message) for message in messages]
    tool_compressed = 0
    for message in fitted:
        if message.get("role") != "tool":
            continue
        before = message.get("content")
        after = _compact_tool_content(before, tool_result_tokens)
        if after != before:
            message["content"] = after
            tool_compressed += 1

    used_after = estimate_messages_tokens(fitted)
    dropped = 0
    if used_after > context_tokens:
        prefix, units = _split_units(fitted)
        keep_units: list[list[dict]] = []
        count = 0
        for unit in reversed(units):
            keep_units.insert(0, unit)
            count += len(unit)
            if count >= max(keep_recent_messages, 1):
                break
        dropped = len(units) - len(keep_units)
        if dropped > 0:
            note = {"role": "system",
                    "content": "【上下文预算】较早的对话与工具结果已压缩或省略。"}
            fitted = [*prefix, note, *[m for unit in keep_units for m in unit]]
            used_after = estimate_messages_tokens(fitted)

    return ContextFit(
        messages=fitted,
        used_before=used_before,
        used_after=used_after,
        compressed=(tool_compressed > 0 or dropped > 0),
        over_budget=used_after > context_tokens,
        tool_compressed=tool_compressed,
        dropped_units=dropped,
    )
