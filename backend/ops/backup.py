"""备份/恢复（M9）。

备份内容：SQLite 一致性快照 + catalog/datasets/reports 目录 + manifest（sha256）。
恢复默认写入新的目标目录（避免误覆盖），校验路径穿越与每个文件哈希。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings

BACKUP_VERSION = 1
INCLUDE_DIRS = ("catalog", "datasets", "reports")


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _data_root(settings: Settings) -> Path:
    return Path(settings.db_path).resolve().parent


def _sqlite_snapshot(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _counts(db_path: Path) -> dict:
    counts = {"catalog_instruments": 0, "datasets": 0, "reports": 0}
    if not db_path.is_file():
        return counts
    conn = sqlite3.connect(str(db_path))
    try:
        for label, table in (("catalog_instruments", "instruments"),
                             ("datasets", "datasets"),
                             ("reports", "reports")):
            try:
                counts[label] = int(conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                pass
    finally:
        conn.close()
    return counts


def create_backup(settings: Settings, target_dir: str | Path | None = None) -> Path:
    data_root = _data_root(settings)
    target = Path(target_dir).resolve() if target_dir else data_root / "backups"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = target / f"oh-ecofin-backup-{stamp}.zip"

    with tempfile.TemporaryDirectory(prefix="oh-ecofin-backup-") as temp:
        temp_dir = Path(temp)
        snapshot = temp_dir / "finagent.db"
        _sqlite_snapshot(Path(settings.db_path), snapshot)
        entries: list[dict] = []
        files_to_zip: list[tuple[Path, str]] = [(snapshot, "finagent.db")]
        for directory in INCLUDE_DIRS:
            path = data_root / directory
            if path.is_dir():
                for file_path in path.rglob("*"):
                    if file_path.is_file() and not any(
                            part in (".tmp", "__pycache__") for part in file_path.parts):
                        files_to_zip.append(
                            (file_path, file_path.relative_to(data_root).as_posix()))
        for path, arcname in files_to_zip:
            entries.append({
                "path": arcname,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            })
        manifest = {
            "version": BACKUP_VERSION,
            "app": "oh-ecofin-agent",
            "created_at": _now(),
            "db": "finagent.db",
            "counts": _counts(snapshot),
            "files": entries,
        }
        manifest_path = temp_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(manifest_path, "manifest.json")
            for path, arcname in files_to_zip:
                zf.write(path, arcname)
    return archive


def verify_backup(archive: str | Path) -> dict:
    path = Path(archive).resolve()
    if not path.is_file():
        raise ValueError(f"备份文件不存在: {path}")
    with zipfile.ZipFile(path) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (KeyError, json.JSONDecodeError) as e:
            raise ValueError("备份缺少合法 manifest.json") from e
        if manifest.get("version") != BACKUP_VERSION:
            raise ValueError(f"不支持的备份版本: {manifest.get('version')}")
        files = manifest.get("files") or []
        names = set(zf.namelist())
        for item in files:
            rel = item.get("path", "")
            if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                raise ValueError(f"备份包含非法路径: {rel}")
            if rel not in names:
                raise ValueError(f"备份缺少文件: {rel}")
            digest = hashlib.sha256(zf.read(rel)).hexdigest()
            if digest != item.get("sha256"):
                raise ValueError(f"备份文件哈希不匹配: {rel}")
    return manifest


def restore_backup(archive: str | Path, target_data_dir: str | Path, *,
                   force: bool = False) -> dict:
    manifest = verify_backup(archive)
    target = Path(target_data_dir).resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise ValueError(f"目标目录非空: {target}（使用 force=True 覆盖）")
    staging = target.parent / f".restore_{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(Path(archive)) as zf:
            for name in zf.namelist():
                if name == "manifest.json":
                    continue
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError(f"备份包含非法路径: {name}")
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        if target.exists():
            old = target.parent / f".old_{uuid.uuid4().hex}"
            target.rename(old)
            staging.rename(target)
            shutil.rmtree(old, ignore_errors=True)
        else:
            staging.rename(target)
        db_path = target / manifest.get("db", "finagent.db")
        return {
            "ok": True,
            "target": str(target),
            "created_at": manifest.get("created_at", ""),
            "files": len(manifest.get("files") or []),
            "counts": _counts(db_path),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
