"""报告数字抽取与单元格数值解析（M7）。

规则：
- 先屏蔽 HTML/日期/时间/年份/列表序号，避免把日期、代码、列表编号当数字；
- 支持千分位、正负号、百分比、万/亿/千；
- 纯 6 位整数（股票代码）与 1900–2100 的裸年份不视为报告数字。
"""

from __future__ import annotations

import math
import re

SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)
STYLE_RE = re.compile(r"<style\b.*?</style>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
DATETIME_RE = re.compile(r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)")
MONTH_RE = re.compile(r"(?<!\d)\d{4}[-/]\d{1,2}(?!\d)")
TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*年")
LIST_RE = re.compile(r"^\s*(?:\d+[.、)]|[（(]\d+[）)])\s*", re.MULTILINE)

# 只排除 ASCII 单词字符，允许中文前缀后的数字（如“增长-5%”）
NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(%|％|万|亿|千)?"
)

MULTIPLIER = {"万": 1e4, "亿": 1e8, "千": 1e3}


def mask_non_claims(text: str) -> str:
    masked = SCRIPT_RE.sub(" ", text or "")
    masked = STYLE_RE.sub(" ", masked)
    masked = TAG_RE.sub(" ", masked)
    masked = DATETIME_RE.sub(" ", masked)
    masked = MONTH_RE.sub(" ", masked)
    masked = TIME_RE.sub(" ", masked)
    masked = YEAR_RE.sub(" ", masked)
    masked = LIST_RE.sub(" ", masked)
    return masked


def _ignored_integer(raw: str, value: float, unit: str) -> bool:
    if unit or "." in raw:
        return False
    digits = raw.lstrip("+-")
    if len(digits) == 6 and digits.isdigit():
        return True
    return 1900 <= value <= 2100 and value.is_integer()


def extract_claims(text: str) -> list[dict]:
    masked = mask_non_claims(text)
    claims: list[dict] = []
    for match in NUMBER_RE.finditer(masked):
        raw, unit = match.group(1), match.group(2) or ""
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if _ignored_integer(raw, value, unit):
            continue
        if unit in ("%", "％"):
            unit = "%"
        elif unit in MULTIPLIER:
            value *= MULTIPLIER[unit]
        start = max(0, match.start() - 16)
        end = min(len(masked), match.end() + 16)
        claims.append({
            "claim": match.group(0).strip(),
            "value": value,
            "unit": unit,
            "context": masked[start:end].strip(),
        })
    return claims


def parse_cell_number(value: object) -> tuple[float, str] | None:
    """单元格值 → (数值, 单位)；无法解析返回 None。

    百分比字符串（如 "53.85%"）转成 0.5385，文本声明 53.85% 时会用 /100 匹配。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return (number, "") if math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    unit = ""
    if text.endswith("%") or text.endswith("％"):
        unit = "%"
        text = text[:-1].strip()
    elif text[-1:] in MULTIPLIER:
        unit = text[-1]
        text = text[:-1].strip()
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    if unit == "%":
        number /= 100
    elif unit in MULTIPLIER:
        number *= MULTIPLIER[unit]
    return number, unit


def claim_candidates(value: float, unit: str) -> list[float]:
    candidates = [float(value)]
    if unit == "%":
        candidates.append(float(value) / 100)
    return candidates
