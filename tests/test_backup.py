"""M9 备份/恢复：往返一致、非空目标保护、篡改与路径穿越拒绝。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from backend.config import Settings
from backend.db import Database
from backend.ops.backup import create_backup, restore_backup, verify_backup


def _seed(root: Path) -> Settings:
    settings = Settings(db_path=str(root / "finagent.db"))
    db = Database(settings.db_path)
    db.execute(
        "INSERT INTO instruments(id, symbol, name, exchange) VALUES(?,?,?,?)",
        ("CN:600519", "600519.SS", "Kweichow Moutai", "SHH"))
    db.execute(
        "INSERT INTO datasets(id, workspace_id, name, table_name, source, created_at)"
        " VALUES(?,?,?,?,?,?)",
        ("ds_test", "local", "sales", "sales", "upload", "2026-11-27 10:00:00"))
    db.execute(
        "INSERT INTO reports(id, title, status, spec_json, created_at)"
        " VALUES(?,?,?,?,?)",
        ("rpt_test", "测试报告", "ok", "{}", "2026-11-27 10:00:00"))
    for directory, name, content in (
            ("catalog", "snapshot.json", b'{"source":"fixture"}'),
            ("datasets", "data.parquet", b"PAR1-test-bytes"),
            ("reports", "report.html", b"<html>report</html>")):
        path = root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return settings


def test_backup_restore_roundtrip(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    settings = _seed(root)

    archive = create_backup(settings, tmp_path / "backups")
    assert archive.is_file()

    manifest = verify_backup(archive)
    assert manifest["app"] == "oh-ecofin-agent"
    assert manifest["counts"] == {
        "catalog_instruments": 1, "datasets": 1, "reports": 1}
    paths = {item["path"] for item in manifest["files"]}
    assert {"finagent.db", "catalog/snapshot.json",
            "datasets/data.parquet", "reports/report.html"} <= paths

    restored_root = tmp_path / "restored"
    result = restore_backup(archive, restored_root)
    assert result["ok"] is True
    assert result["counts"] == manifest["counts"]
    assert (restored_root / "reports" / "report.html").read_bytes() == \
        b"<html>report</html>"

    conn = sqlite3.connect(str(restored_root / "finagent.db"))
    try:
        assert conn.execute(
            "SELECT name FROM instruments WHERE id='CN:600519'").fetchone()[0] \
            == "Kweichow Moutai"
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
    finally:
        conn.close()


def test_restore_refuses_nonempty_target(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    settings = _seed(root)
    archive = create_backup(settings, tmp_path / "backups")

    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="目标目录非空"):
        restore_backup(archive, target)
    assert (target / "keep.txt").is_file()

    result = restore_backup(archive, target, force=True)
    assert result["ok"] is True
    assert (target / "finagent.db").is_file()


def test_verify_rejects_tampered_archive(tmp_path):
    archive = tmp_path / "tampered.zip"
    manifest = {
        "version": 1, "app": "oh-ecofin-agent",
        "files": [{"path": "data.txt", "sha256": hashlib.sha256(
            b"expected").hexdigest(), "size": 8}],
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("data.txt", "tampered")
    with pytest.raises(ValueError, match="哈希不匹配"):
        verify_backup(archive)


def test_verify_rejects_manifest_path_traversal(tmp_path):
    archive = tmp_path / "traversal.zip"
    payload = b"evil"
    manifest = {
        "version": 1, "app": "oh-ecofin-agent",
        "files": [{"path": "../evil.txt",
                   "sha256": hashlib.sha256(payload).hexdigest(),
                   "size": len(payload)}],
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("../evil.txt", payload)
    with pytest.raises(ValueError, match="非法路径"):
        verify_backup(archive)


def test_restore_rejects_extra_traversal_member(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    settings = _seed(root)
    archive = create_backup(settings, tmp_path / "backups")

    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("../escape.txt", b"escape")

    with pytest.raises(ValueError, match="非法路径"):
        restore_backup(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_verify_requires_manifest(tmp_path):
    archive = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("data.txt", "x")
    with pytest.raises(ValueError, match="manifest"):
        verify_backup(archive)
