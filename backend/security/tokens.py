"""局域网访问令牌：随机生成 + 常量时间比较校验。

局域网模式必须显式开启并携带随机 token（URL ?t= 校验），永不默认 0.0.0.0。
"""

from __future__ import annotations

import hmac
import secrets


def generate_token(n_bytes: int = 24) -> str:
    return secrets.token_urlsafe(n_bytes)


def verify_token(provided: str | None, expected: str | None) -> bool:
    """常量时间比较；expected 为空视为未开启校验（仅 127.0.0.1 场景）。"""
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)
