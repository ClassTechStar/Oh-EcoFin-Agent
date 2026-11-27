"""导入 FinanceDatabase 标的目录快照（M1）。

用法：
    python scripts/import_catalog.py --source C:\\path\\to\\FinanceDatabase
    python scripts/import_catalog.py --source ... --as-of 2026-11-27 --force

数据来源为 FinanceDatabase（MIT，https://github.com/JerBouma/FinanceDatabase），
只导入目录字段；快照 manifest 保留来源、许可与内容哈希。同一内容幂等。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.catalog.importer import import_financedatabase  # noqa: E402
from backend.catalog.models import CatalogError  # noqa: E402
from backend.catalog.store import CatalogStore  # noqa: E402
from backend.config import load_settings  # noqa: E402
from backend.db import Database  # noqa: E402


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="导入 FinanceDatabase 目录快照")
    parser.add_argument("--source", required=True,
                        help="FinanceDatabase 仓库根目录（含 compression/ 或 database/）")
    parser.add_argument("--db", default=settings.db_path, help="SQLite 元数据库路径")
    parser.add_argument("--catalog-dir", default=settings.catalog.dir,
                        help="快照 manifest 落盘目录")
    parser.add_argument("--as-of", default="", help="快照生效日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--source-version", default="", help="来源版本标签")
    parser.add_argument("--force", action="store_true",
                        help="忽略内容哈希幂等检查，强制重导")
    args = parser.parse_args()

    store = CatalogStore(Database(args.db))
    try:
        result = import_financedatabase(
            args.source, store, args.catalog_dir, as_of=args.as_of,
            source_version=args.source_version, force=args.force)
    except CatalogError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        if e.hint:
            print(f"[HINT] {e.hint}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n目录总数: {store.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
