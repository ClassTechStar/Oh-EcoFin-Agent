"""开箱内置种子数据：迷你标的目录（首次启动自动导入，60 秒内看到第一根 K 线）。"""

from __future__ import annotations

import json
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent

#: 迷你目录内置标的（与 tests/cassettes_frozen 演示白名单同源）
MINI_CATALOG_PATH = SEED_DIR / "mini_catalog.json"


def load_mini_catalog() -> dict:
    return json.loads(MINI_CATALOG_PATH.read_text(encoding="utf-8"))
