"""数据集元数据存储（M2）：datasets + dataset_versions。

版本语义：数据集以名称（slug 哈希）为稳定 ID，每次内容变化 version+1；
同一 sha256 重复导入由 service 层直接返回既有版本（幂等）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend.datasets.models import DatasetManifest
from backend.db import Database


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class DatasetStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 写入 ----------

    def create_dataset(self, dataset_id: str, name: str, table_name: str,
                       source: str = "upload") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO datasets(id, workspace_id, name, table_name, source,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (dataset_id, "local", name, table_name, source, _now()))

    def add_version(self, manifest: DatasetManifest, path: str) -> None:
        version_id = f"{manifest.dataset_id}:v{manifest.version}"
        self.db.execute(
            "INSERT OR REPLACE INTO dataset_versions"
            "(id, dataset_id, version, sha256, manifest_json, path, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (version_id, manifest.dataset_id, manifest.version, manifest.sha256,
             manifest.model_dump_json(by_alias=True), path,
             manifest.created_at or _now()))

    # ---------- 读取 ----------

    def get_dataset(self, dataset_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM datasets WHERE id=?", (dataset_id,))
        return dict(row) if row else None

    def find_by_sha(self, sha256: str) -> dict | None:
        row = self.db.query_one(
            "SELECT v.*, d.name, d.table_name, d.source FROM dataset_versions v"
            " JOIN datasets d ON d.id=v.dataset_id WHERE v.sha256=?"
            " ORDER BY v.created_at ASC LIMIT 1", (sha256,))
        return self._version_payload(row) if row else None

    def get_version(self, dataset_id: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self.db.query_one(
                "SELECT v.*, d.name, d.table_name, d.source FROM dataset_versions v"
                " JOIN datasets d ON d.id=v.dataset_id"
                " WHERE v.dataset_id=? ORDER BY v.version DESC LIMIT 1", (dataset_id,))
        else:
            row = self.db.query_one(
                "SELECT v.*, d.name, d.table_name, d.source FROM dataset_versions v"
                " JOIN datasets d ON d.id=v.dataset_id"
                " WHERE v.dataset_id=? AND v.version=?", (dataset_id, version))
        return self._version_payload(row) if row else None

    def latest_versions(self) -> list[dict]:
        rows = self.db.query(
            "SELECT v.*, d.name, d.table_name, d.source FROM dataset_versions v"
            " JOIN datasets d ON d.id=v.dataset_id"
            " WHERE v.version=(SELECT MAX(version) FROM dataset_versions"
            "                  WHERE dataset_id=d.id)"
            " ORDER BY d.created_at DESC, d.id")
        return [self._version_payload(r) for r in rows]

    def versions(self, dataset_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT v.*, d.name, d.table_name, d.source FROM dataset_versions v"
            " JOIN datasets d ON d.id=v.dataset_id"
            " WHERE v.dataset_id=? ORDER BY v.version DESC", (dataset_id,))
        return [self._version_payload(r) for r in rows]

    def next_version(self, dataset_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(version),0) AS v FROM dataset_versions WHERE dataset_id=?",
            (dataset_id,))
        return int(row["v"]) + 1 if row else 1

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM datasets")
        return int(row["c"]) if row else 0

    @staticmethod
    def _version_payload(row: dict) -> dict:
        try:
            manifest = json.loads(row.get("manifest_json") or "{}")
        except json.JSONDecodeError:
            manifest = {}
        return {
            "dataset_id": row["dataset_id"],
            "name": row.get("name", ""),
            "table_name": row.get("table_name", ""),
            "source": row.get("source", ""),
            "version": int(row.get("version") or 1),
            "sha256": row.get("sha256", ""),
            "path": row.get("path", ""),
            "created_at": row.get("created_at", ""),
            "manifest": manifest,
        }
