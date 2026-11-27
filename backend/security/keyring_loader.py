"""密钥三级读取：环境变量 → Windows 凭据管理器(keyring) → 本地 .env。

任一级命中即用；三级皆空 → 启动时明确报错指引，
绝不把"无密钥"伪装成"模型故障"。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "oh-ecofin-agent"

ENV_FALLBACK_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

#: 密钥名 → 环境变量名映射
KEY_ENV_MAP = {
    "glm_key": "FINAGENT_GLM_KEY",
    "deepseek_key": "FINAGENT_DEEPSEEK_KEY",
    "bocha_key": "FINAGENT_BOCHA_KEY",
    "zhipu_search_key": "FINAGENT_ZHIPU_SEARCH_KEY",
    "lan_token": "FINAGENT_LAN_TOKEN",
}


def _read_dotenv(key: str) -> str:
    """从项目根 .env 读取（开发兜底；生产/答辩机走 keyring）。"""
    if not ENV_FALLBACK_FILE.exists():
        return ""
    for line in ENV_FALLBACK_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def load_key(name: str) -> str:
    """三级读取（按优先级）：env → keyring → .env。命中即返回。"""
    env_name = KEY_ENV_MAP.get(name, f"FINAGENT_{name.upper()}")
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    try:
        import keyring

        val = (keyring.get_password(KEYRING_SERVICE, name) or "").strip()
        if val:
            return val
    except Exception:  # keyring 后端不可用（Linux 无 dbus 等）→ 静默降级
        logger.debug("keyring 不可用，跳过 %s", name)
    return _read_dotenv(env_name)


def missing_required_keys() -> list[str]:
    """返回缺失的关键密钥名（启动健康检查用）。"""
    required = ["glm_key", "deepseek_key"]
    return [k for k in required if not load_key(k)]
