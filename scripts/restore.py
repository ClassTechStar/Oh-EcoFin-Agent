"""恢复备份（M9）：默认恢复到新目录，校验 manifest 与文件哈希。

用法：
    python scripts/restore.py --archive data/backups/xxx.zip --target C:\\restore-data
    python scripts/restore.py --archive ... --target ... --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.ops.backup import restore_backup, verify_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复 Oh-EcoFin-Agent 数据备份")
    parser.add_argument("--archive", required=True, help="备份 zip 路径")
    parser.add_argument("--target", required=True, help="恢复目标数据目录")
    parser.add_argument("--force", action="store_true", help="目标非空时覆盖")
    parser.add_argument("--verify-only", action="store_true", help="只校验不恢复")
    args = parser.parse_args()
    if args.verify_only:
        manifest = verify_backup(args.archive)
        print(json.dumps({"ok": True, "manifest": manifest},
                         ensure_ascii=False, indent=2))
        return 0
    result = restore_backup(args.archive, args.target, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
