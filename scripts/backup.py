"""创建备份（M9）：SQLite 一致性快照 + catalog/datasets/reports + manifest。

用法：
    python scripts/backup.py
    python scripts/backup.py --target C:\\backups
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import load_settings  # noqa: E402
from backend.ops.backup import create_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 Oh-EcoFin-Agent 数据备份")
    parser.add_argument("--target", default="", help="备份输出目录（默认 data/backups）")
    args = parser.parse_args()
    archive = create_backup(load_settings(), args.target or None)
    print(json.dumps({"ok": True, "archive": str(archive),
                      "bytes": archive.stat().st_size}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
