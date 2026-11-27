"""DuckDB 只读查询引擎（M3）。

工作区：把每个数据集的最新 Parquet 版本物化成一个 DuckDB 表，文件按
表集合指纹命名（`query_<fp>.duckdb`），查询连接 `read_only=True` 且关闭
外部访问，实现“只读 + 无文件逃逸”的双重边界。

执行限制：`fetchmany(max_rows+1)` 控制输出行数；`conn.interrupt()` 定时器
中断超时查询；每次查询（成功/失败）都写 query_audit。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from backend.datasets.service import DatasetService
from backend.db import Database
from backend.query.guard import SqlGuardError, validate_sql

MAX_AUDIT_SQL_CHARS = 20000


class QueryExecutionError(ValueError):
    def __init__(self, message: str, *, code: str = "execution_error",
                 hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class QueryTimeoutError(QueryExecutionError):
    def __init__(self, message: str = "查询超时", hint: str = ""):
        super().__init__(message, code="timeout", hint=hint)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()[:128]
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


class QueryAuditStore:
    def __init__(self, db: Database):
        self.db = db

    def record(self, *, dataset_ids: list[str], sql_text: str, sql_hash: str,
               result_hash: str, rows: int, elapsed_ms: int, ok: bool,
               error: str = "") -> int:
        from backend.db import _now

        return self.db.execute(
            "INSERT INTO query_audit(dataset_ids_json, sql_text, sql_hash,"
            " result_hash, rows, elapsed_ms, ok, error, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (json.dumps(dataset_ids, ensure_ascii=False), sql_text[:MAX_AUDIT_SQL_CHARS],
             sql_hash, result_hash, rows, elapsed_ms, int(ok), error[:1000], _now()))

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM query_audit ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            try:
                dataset_ids = json.loads(row.get("dataset_ids_json") or "[]")
            except json.JSONDecodeError:
                dataset_ids = []
            out.append({
                "id": row["id"],
                "dataset_ids": dataset_ids,
                "sql": row["sql_text"],
                "sql_hash": row["sql_hash"],
                "result_hash": row["result_hash"],
                "rows": row["rows"],
                "elapsed_ms": row["elapsed_ms"],
                "ok": bool(row["ok"]),
                "error": row["error"],
                "created_at": row["created_at"],
            })
        return out


class QueryEngine:
    def __init__(self, datasets: DatasetService, audit: QueryAuditStore,
                 workspace_dir: str | Path, *, max_rows: int = 100_000,
                 timeout_seconds: float = 30.0):
        self.datasets = datasets
        self.audit = audit
        self.workspace_dir = Path(workspace_dir)
        self.max_rows = int(max_rows)
        self.timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()

    # ---------- 工作区 ----------

    def tables(self) -> list[dict]:
        return [
            {
                "table_name": v["table_name"],
                "dataset_id": v["dataset_id"],
                "name": v["name"],
                "version": v["version"],
                "rows": (v.get("manifest") or {}).get("rows", 0),
                "columns": (v.get("manifest") or {}).get("columns", 0),
                "sha256": v["sha256"],
            }
            for v in self.datasets.store.latest_versions()
            if v.get("table_name")
        ]

    def _workspace(self, table_map: dict[str, dict]) -> Path:
        payload = json.dumps(
            {name: info["path"] for name, info in table_map.items()},
            sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        path = self.workspace_dir / f"query_{fingerprint}.duckdb"
        if path.is_file():
            return path
        with self._lock:
            if path.is_file():
                return path
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            building = self.workspace_dir / f".build_{uuid.uuid4().hex}.duckdb"
            conn = duckdb.connect(str(building))
            try:
                for table_name, info in sorted(table_map.items()):
                    escaped_table = table_name.replace('"', '""')
                    escaped_path = str(info["path"]).replace("'", "''")
                    conn.execute(
                        f'CREATE TABLE "{escaped_table}" AS'
                        f" SELECT * FROM read_parquet('{escaped_path}')")
            finally:
                conn.close()
            os.replace(building, path)
            self._cleanup(keep=path.name)
        return path

    def _cleanup(self, keep: str) -> None:
        try:
            for item in self.workspace_dir.glob("query_*.duckdb*"):
                if item.name != keep:
                    item.unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 执行 ----------

    def execute(self, sql: str, *, max_rows: int | None = None,
                dataset_ids: list[str] | None = None) -> dict:
        started = time.perf_counter()
        limit = min(int(max_rows or self.max_rows), self.max_rows)
        sql_hash = _sql_hash(sql)
        try:
            guarded = validate_sql(sql)
        except SqlGuardError as e:
            elapsed = int((time.perf_counter() - started) * 1000)
            self.audit.record(dataset_ids=dataset_ids or [], sql_text=sql,
                              sql_hash=sql_hash, result_hash="", rows=0,
                              elapsed_ms=elapsed, ok=False,
                              error=f"{e.code}: {e}")
            raise

        table_map = self.datasets.table_map()
        if dataset_ids:
            allowed = set(dataset_ids)
            table_map = {
                name: info for name, info in table_map.items()
                if info["dataset_id"] in allowed
            }
            if not table_map:
                raise QueryExecutionError(
                    f"指定的数据集不存在: {sorted(allowed)}", code="dataset_not_found",
                    hint="先调用 dataset_list 查看可用数据集")
        table_ids = sorted({info["dataset_id"] for info in table_map.values()})
        available = sorted(table_map)
        try:
            workspace = self._workspace(table_map)
            conn = duckdb.connect(str(workspace), read_only=True)
        except (duckdb.Error, OSError) as e:
            elapsed = int((time.perf_counter() - started) * 1000)
            self.audit.record(
                dataset_ids=table_ids, sql_text=guarded, sql_hash=sql_hash,
                result_hash="", rows=0, elapsed_ms=elapsed, ok=False,
                error=f"workspace_error: {e}")
            raise QueryExecutionError(
                f"查询工作区构建失败: {e}", code="workspace_error",
                hint="重新导入数据集，或检查 data/workspaces 写权限") from e
        timer: threading.Timer | None = None
        try:
            conn.execute("SET enable_external_access=false")
            timer = threading.Timer(self.timeout_seconds, conn.interrupt)
            timer.daemon = True
            timer.start()
            cursor = conn.execute(guarded)
            columns = [str(d[0]) for d in cursor.description] if cursor.description else []
            fetched = cursor.fetchmany(limit + 1)
            truncated = len(fetched) > limit
            rows = [
                [_json_value(value) for value in row]
                for row in fetched[:limit]
            ]
            elapsed = int((time.perf_counter() - started) * 1000)
            result_hash = hashlib.sha256(
                json.dumps({"columns": columns, "rows": rows},
                           ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()[:16]
            self.audit.record(
                dataset_ids=table_ids, sql_text=guarded, sql_hash=sql_hash,
                result_hash=result_hash, rows=len(rows), elapsed_ms=elapsed,
                ok=True)
            return {
                "ok": True,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "max_rows": limit,
                "elapsed_ms": elapsed,
                "sql_hash": sql_hash,
                "result_hash": result_hash,
                "datasets": table_ids,
                "tables": available,
            }
        except (duckdb.InterruptException, duckdb.Error) as e:
            elapsed = int((time.perf_counter() - started) * 1000)
            if isinstance(e, duckdb.InterruptException) or "interrupt" in str(e).lower():
                message = f"查询超时（>{self.timeout_seconds:g}s）"
                hint = "缩小扫描范围、增加过滤条件，或调大 FINAGENT_DATASETS__QUERY_TIMEOUT_SECONDS"
                self.audit.record(dataset_ids=table_ids, sql_text=guarded,
                                  sql_hash=sql_hash, result_hash="", rows=0,
                                  elapsed_ms=elapsed, ok=False, error=message)
                raise QueryTimeoutError(message, hint=hint) from e
            message = str(e).splitlines()[0][:500]
            hint = "可用表: " + ", ".join(f'"{name}"' for name in available) if available \
                else "还没有数据集，请先导入 CSV/Excel/Parquet"
            self.audit.record(dataset_ids=table_ids, sql_text=guarded,
                              sql_hash=sql_hash, result_hash="", rows=0,
                              elapsed_ms=elapsed, ok=False, error=message)
            raise QueryExecutionError(message, hint=hint) from e
        finally:
            if timer is not None:
                timer.cancel()
            conn.close()
