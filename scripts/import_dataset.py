"""导入本地数据集（CSV/Excel/Parquet）→ 不可变 Parquet 快照（M2）。

用法：
    python scripts/import_dataset.py --path data/sample.csv --name sales

同一内容重复导入幂等；同名不同内容自动 version+1。限额来自配置：
FINAGENT_DATASETS__MAX_FILE_MB / MAX_ROWS / MAX_COLUMNS。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import load_settings  # noqa: E402
from backend.datasets.models import DatasetError  # noqa: E402
from backend.datasets.service import DatasetService  # noqa: E402
from backend.db import Database  # noqa: E402


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="导入本地数据集为不可变 Parquet 快照")
    parser.add_argument("--path", required=True, help="CSV/TSV/XLSX/Parquet 文件")
    parser.add_argument("--name", default="", help="数据集名（默认取文件名）")
    parser.add_argument("--db", default=settings.db_path, help="SQLite 元数据库路径")
    parser.add_argument("--datasets-dir", default=settings.datasets.dir,
                        help="Parquet 快照根目录")
    args = parser.parse_args()

    settings.db_path = args.db
    settings.datasets.dir = args.datasets_dir
    service = DatasetService(Database(args.db), settings)
    try:
        result = service.import_path(args.path, name=args.name)
    except DatasetError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        if e.hint:
            print(f"[HINT] {e.hint}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
