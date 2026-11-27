"""日志脱敏：logging Filter 正则掩蔽 api_key / Authorization / 手机号。

工具原始返回落库前同规则处理。
"""

from __future__ import annotations

import re

MASK = "***MASKED***"

# 显式键值对形式：api_key=xxx / key: xxx / token=xxx
_KEYVAL = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|authorization)\s*[:=]\s*)(\S{8,})"
)
# sk- 开头的密钥
_SK = re.compile(r"(sk-[A-Za-z0-9_-]{8,})")
# Bearer 令牌
_BEARER = re.compile(r"(Bearer\s+)([A-Za-z0-9._-]{8,})")
# 中国大陆手机号（11 位，1 开头，前后非数字）
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


def mask_text(text: str) -> str:
    """对任意文本做密钥/手机号掩蔽。"""
    if not text:
        return text
    out = _KEYVAL.sub(lambda m: m.group(1) + MASK, text)
    out = _SK.sub(MASK, out)
    out = _BEARER.sub(lambda m: m.group(1) + MASK, out)
    out = _PHONE.sub(MASK, out)
    return out


class SensitiveFilter:
    """logging.Filter：对 message 与 args 统一脱敏（非字符串参数原样保留）。"""

    def filter(self, record) -> bool:  # noqa: A003
        try:
            record.msg = mask_text(str(record.msg))
            if record.args:
                record.args = tuple(
                    mask_text(a) if isinstance(a, str) else a for a in record.args)
        except Exception:
            pass
        return True


def mask_result_for_storage(result) -> object:
    """工具原始返回落库前脱敏（递归处理字符串字段）。"""
    if isinstance(result, str):
        return mask_text(result)
    if isinstance(result, dict):
        return {k: mask_result_for_storage(v) for k, v in result.items()}
    if isinstance(result, list):
        return [mask_result_for_storage(v) for v in result]
    return result
