"""数据集导入服务（M2）。

流程：源文件 → 类型识别（UTF-8/GBK 自适应）→ DuckDB 载入 → 限额校验
→ 不可变 Parquet（ZSTD）→ sha256 → manifest.json → 元数据入库。

安全边界：文件名只取 basename；所有路径由服务端拼接；原始文件仅作只读输入。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import tempfile
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

import duckdb
import pandas as pd

from backend.config import Settings
from backend.datasets.models import (
    DatasetImportError,
    DatasetLimitError,
    DatasetManifest,
    DatasetNotFoundError,
)
from backend.datasets.store import DatasetStore, _now
from backend.db import Database

_SUFFIXES = {".csv", ".tsv", ".parquet", ".pq", ".xlsx", ".xlsm", ".xltx"}
_NUMERIC = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|"
    r"UBIGINT|FLOAT|REAL|DOUBLE|DECIMAL)")
_SLUG = re.compile(r"[^a-z0-9]+")


def _escaped(path: Path) -> str:
    return str(path).replace("'", "''")


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()[:128]
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def slugify(text: str) -> str:
    slug = _SLUG.sub("_", str(text or "").strip().lower()).strip("_")
    if not slug:
        slug = "dataset"
    if slug[0].isdigit():
        slug = f"t_{slug}"
    return slug[:48]


def _dedupe_columns(columns: list[Any]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in columns:
        name = str(raw).strip() or "column"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


class DatasetService:
    def __init__(self, db: Database, settings: Settings):
        self.settings = settings
        self.store = DatasetStore(db)
        self.dataset_dir = Path(settings.datasets.dir)
        self.workspace_dir = Path(settings.datasets.workspace_dir)
        self.tmp_dir = self.dataset_dir / ".tmp"
        self.max_bytes = int(settings.datasets.max_file_mb) * 1024 * 1024

    # ---------- 导入 ----------

    def import_path(self, path: str | Path, name: str = "",
                    source_uri: str = "") -> dict:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise DatasetImportError(f"文件不存在: {source}")
        return self._import_file(source, name or source.stem,
                                 source_uri or source.name)

    def import_upload(self, fileobj: BinaryIO, filename: str) -> dict:
        safe_name = Path(str(filename or "upload.csv")).name
        suffix = Path(safe_name).suffix.lower()
        self._check_suffix(suffix)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix="upload_", dir=self.tmp_dir).name)
        total = 0
        try:
            with tmp.open("wb") as dst:
                while chunk := fileobj.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise DatasetLimitError(
                            f"文件超过限额: {self.max_bytes // (1024 * 1024)} MB",
                            hint="拆分文件后重试，或调大 FINAGENT_DATASETS__MAX_FILE_MB")
                    dst.write(chunk)
            if total == 0:
                raise DatasetImportError("上传文件为空")
            return self._import_file(tmp, Path(safe_name).stem, safe_name,
                                     cleanup=True)
        finally:
            tmp.unlink(missing_ok=True)

    def _check_suffix(self, suffix: str) -> None:
        if suffix == ".xls":
            raise DatasetImportError(
                "暂不支持旧版 .xls，请另存为 .xlsx",
                hint="Excel → 另存为 Excel 工作簿（.xlsx）后重试")
        if suffix not in _SUFFIXES:
            raise DatasetImportError(
                f"不支持的文件类型: {suffix or '(无扩展名)'}",
                hint="支持 CSV/TSV/Parquet/XLSX")

    def _import_file(self, source: Path, name: str, source_uri: str,
                     cleanup: bool = False) -> dict:
        suffix = source.suffix.lower()
        self._check_suffix(suffix)
        size = source.stat().st_size
        if size > self.max_bytes:
            raise DatasetLimitError(
                f"文件超过限额: {size / 1024 / 1024:.1f} MB > "
                f"{self.max_bytes // (1024 * 1024)} MB",
                hint="拆分文件后重试，或调大 FINAGENT_DATASETS__MAX_FILE_MB")

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        parquet_tmp = self.tmp_dir / f"snapshot_{uuid.uuid4().hex}.parquet"
        conn = duckdb.connect()
        converted: list[Path] = []
        try:
            table = self._load_source(conn, source, suffix, converted)
            rows = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            desc = conn.execute(f"DESCRIBE {table}").fetchall()
            columns = [(str(r[0]), str(r[1])) for r in desc]
            if rows > int(self.settings.datasets.max_rows):
                raise DatasetLimitError(
                    f"行数超过限额: {rows} > {self.settings.datasets.max_rows}",
                    hint="拆分文件后重试，或调大 FINAGENT_DATASETS__MAX_ROWS")
            if len(columns) > int(self.settings.datasets.max_columns):
                raise DatasetLimitError(
                    f"列数超过限额: {len(columns)} > {self.settings.datasets.max_columns}",
                    hint="裁剪列后重试，或调大 FINAGENT_DATASETS__MAX_COLUMNS")
            if not columns:
                raise DatasetImportError("数据源没有可识别的列")
            conn.execute(
                f"COPY {table} TO '{_escaped(parquet_tmp)}'"
                " (FORMAT PARQUET, COMPRESSION ZSTD)")
        except duckdb.Error as e:
            raise DatasetImportError(
                f"数据解析失败: {e}", hint="检查表头/编码/分隔符后重试") from e
        finally:
            conn.close()
            for item in converted:
                item.unlink(missing_ok=True)
            if cleanup:
                source.unlink(missing_ok=True)

        sha = _sha256_file(parquet_tmp)
        existing = self.store.find_by_sha(sha)
        if existing:
            parquet_tmp.unlink(missing_ok=True)
            return {**existing, "created": False, "idempotent": True}

        dataset_id = "ds_" + hashlib.sha256(
            slugify(name).encode("utf-8")).hexdigest()[:12]
        dest = self.dataset_dir / sha[:2] / sha[2:4] / sha / "data.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            # 内容已落盘但元数据缺失（如数据库重建）：复用快照，不覆盖不可变文件
            parquet_tmp.unlink(missing_ok=True)
        else:
            shutil.move(str(parquet_tmp), str(dest))

        version = self.store.next_version(dataset_id)
        table_name = self._table_name(name, dataset_id)
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            name=name,
            version=version,
            sha256=sha,
            rows=rows,
            columns=len(columns),
            schema_fields={c: t for c, t in columns},
            source_uri=source_uri,
            table_name=table_name,
            created_at=_now(),
        )
        (dest.parent / "manifest.json").write_text(
            manifest.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        self.store.create_dataset(dataset_id, name, table_name, source="upload")
        self.store.add_version(manifest, str(dest))
        return {
            "dataset_id": dataset_id,
            "name": name,
            "table_name": table_name,
            "version": version,
            "sha256": sha,
            "rows": rows,
            "columns": len(columns),
            "format": "parquet",
            "path": str(dest),
            "manifest": manifest.model_dump(by_alias=True),
            "created": True,
            "idempotent": False,
        }

    def _load_source(self, conn: duckdb.DuckDBPyConnection, source: Path,
                     suffix: str, converted: list[Path]) -> str:
        if suffix in (".parquet", ".pq"):
            conn.execute(
                f"CREATE TABLE data AS SELECT * FROM read_parquet('{_escaped(source)}')")
        elif suffix in (".csv", ".tsv"):
            readable, delimiter, expected_rows = self._prepare_csv(
                source, suffix, converted)
            delim_sql = delimiter.replace("'", "''")
            conn.execute(
                f"CREATE TABLE data AS SELECT * FROM read_csv("
                f"'{_escaped(readable)}', header=true, delim='{delim_sql}',"
                " quote='\"', escape='\"', sample_size=-1, strict_mode=false)")
            actual = int(conn.execute(
                "SELECT COUNT(*) FROM data").fetchone()[0])
            if actual != expected_rows:
                raise DatasetImportError(
                    f"CSV 行数校验失败: 解析 {actual} 行，校验 {expected_rows} 行",
                    hint="检查引号/换行/分隔符是否规范后重试")
        elif suffix in (".xlsx", ".xlsm", ".xltx"):
            df = pd.read_excel(source, sheet_name=0)
            df.columns = _dedupe_columns(list(df.columns))
            conn.register("_upload_df", df)
            conn.execute("CREATE TABLE data AS SELECT * FROM _upload_df")
            conn.unregister("_upload_df")
        else:  # pragma: no cover - _check_suffix 已拦截
            raise DatasetImportError(f"不支持的文件类型: {suffix}")
        return "data"

    def _prepare_csv(self, source: Path, suffix: str,
                     converted: list[Path]) -> tuple[Path, str, int]:
        """编码规整 + 方言/行宽校验，返回 (可读文件, 分隔符, 数据行数)。

        DuckDB 的 CSV 嗅探在“空字段 + CRLF”组合上会误判，因此这里先用 Python
        csv 做全量行宽校验，再以显式分隔符读取；两边行数必须一致。
        """
        data = source.read_bytes()
        if not data:
            raise DatasetImportError("CSV 文件为空")
        path = source
        if data.startswith(b"\xef\xbb\xbf"):
            tmp = Path(tempfile.NamedTemporaryFile(
                delete=False, suffix=".utf8.csv", prefix="upload_").name)
            tmp.write_bytes(data[3:])
            converted.append(tmp)
            path = tmp
            data = data[3:]
        else:
            try:
                data[:65536].decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("gbk", errors="replace")
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                tmp = Path(tempfile.NamedTemporaryFile(
                    delete=False, suffix=".utf8.csv", prefix="upload_").name)
                tmp.write_text(text, encoding="utf-8")
                converted.append(tmp)
                path = tmp

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_bytes().decode("gbk", errors="replace")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            tmp = Path(tempfile.NamedTemporaryFile(
                delete=False, suffix=".utf8.csv", prefix="upload_").name)
            tmp.write_text(text, encoding="utf-8")
            converted.append(tmp)
            path = tmp
        sample = text[:65536]
        delimiter = "\t" if suffix == ".tsv" else ","
        try:
            sniffed = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delimiter = sniffed.delimiter
        except csv.Error:
            pass
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        header = next(reader, None)
        if not header:
            raise DatasetImportError("CSV 缺少表头")
        if any(not str(name).strip() for name in header):
            raise DatasetImportError(
                "CSV 表头存在空列名", hint="为每一列补一个列名后重试")
        if len(set(header)) != len(header):
            duplicates = sorted({name for name in header if header.count(name) > 1})
            raise DatasetImportError(
                f"CSV 表头存在重复列名: {duplicates}",
                hint="重命名重复列后重试")
        rows = 0
        for line_no, row in enumerate(reader, start=2):
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if len(row) != len(header):
                raise DatasetImportError(
                    f"CSV 第 {line_no} 行列数不一致: {len(row)} != {len(header)}",
                    hint="检查多余分隔符、未闭合引号或换行")
            rows += 1
        return path, delimiter, rows

    def _table_name(self, name: str, dataset_id: str) -> str:
        base = slugify(name)
        row = self.store.db.query_one(
            "SELECT id FROM datasets WHERE table_name=?", (base,))
        if row and row["id"] != dataset_id:
            base = f"{base}_{dataset_id[-4:]}"
        return base

    # ---------- 读取 ----------

    def list(self) -> list[dict]:
        return [self._summary(v) for v in self.store.latest_versions()]

    def get(self, dataset_id: str, version: int | None = None) -> dict | None:
        payload = self.store.get_version(dataset_id, version)
        return self._summary(payload) if payload else None

    def table_map(self) -> dict[str, dict]:
        """SQL 查询层的表名 → 最新版本映射。"""
        return {
            v["table_name"]: {**v} for v in self.store.latest_versions()
            if v.get("table_name") and v.get("path")
        }

    @staticmethod
    def _summary(payload: dict) -> dict:
        manifest = payload.get("manifest") or {}
        return {
            "dataset_id": payload["dataset_id"],
            "name": payload["name"],
            "table_name": payload["table_name"],
            "version": payload["version"],
            "sha256": payload["sha256"],
            "rows": manifest.get("rows", 0),
            "columns": manifest.get("columns", 0),
            "schema": manifest.get("schema_json", {}),
            "source_uri": manifest.get("source_uri", ""),
            "created_at": payload["created_at"],
            "path": payload["path"],
        }

    def profile(self, dataset_id: str, version: int | None = None,
                sample_rows: int = 5, max_columns: int = 40) -> dict:
        payload = self.store.get_version(dataset_id, version)
        if not payload:
            raise DatasetNotFoundError(f"数据集不存在: {dataset_id}")
        path = Path(payload["path"])
        if not path.is_file():
            raise DatasetNotFoundError(f"数据集快照文件缺失: {path}")
        sample_rows = max(0, min(int(sample_rows), 50))
        manifest = payload.get("manifest") or {}
        conn = duckdb.connect()
        try:
            rel = f"read_parquet('{_escaped(path)}')"
            total = int(conn.execute(f"SELECT COUNT(*) FROM {rel}").fetchone()[0])
            desc = conn.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
            columns = [(str(r[0]), str(r[1])) for r in desc]
            deep = columns[:max_columns]
            profiles = self._column_profiles(conn, rel, deep, total)
            sample = []
            if sample_rows:
                cursor = conn.execute(f"SELECT * FROM {rel} LIMIT {sample_rows}")
                names = [d[0] for d in cursor.description]
                sample = [
                    {str(n): _json_value(v) for n, v in zip(names, row, strict=False)}
                    for row in cursor.fetchall()
                ]
        except duckdb.Error as e:
            raise DatasetImportError(f"数据集画像失败: {e}") from e
        finally:
            conn.close()
        return {
            "dataset_id": dataset_id,
            "name": payload["name"],
            "version": payload["version"],
            "sha256": payload["sha256"],
            "rows": total,
            "columns": len(columns),
            "schema": manifest.get("schema_json", {}),
            "profile": profiles,
            "profiled_columns": len(deep),
            "truncated_columns": max(0, len(columns) - len(deep)),
            "sample": sample,
        }

    def _column_profiles(self, conn: duckdb.DuckDBPyConnection, rel: str,
                         columns: list[tuple[str, str]], total: int) -> list[dict]:
        exprs: list[str] = []
        layout: list[tuple[str, str, bool]] = []
        numeric_budget = 20
        for name, dtype in columns:
            col = _ident(name)
            is_numeric = bool(_NUMERIC.match(dtype.upper()))
            use_quantile = is_numeric and numeric_budget > 0
            if use_quantile:
                numeric_budget -= 1
            exprs.extend([
                f"COUNT(*) - COUNT({col})",
                f"COUNT(DISTINCT {col})",
                f"MIN(CAST({col} AS VARCHAR))",
                f"MAX(CAST({col} AS VARCHAR))",
            ])
            if use_quantile:
                exprs.extend([
                    f"approx_quantile({col}, 0.25)",
                    f"approx_quantile({col}, 0.50)",
                    f"approx_quantile({col}, 0.75)",
                ])
            layout.append((name, dtype, use_quantile))
        values: tuple = ()
        if exprs:
            values = conn.execute("SELECT " + ", ".join(exprs) + f" FROM {rel}").fetchone()

        profiles: list[dict] = []
        offset = 0
        sample_budget = 10
        for name, dtype, use_quantile in layout:
            nulls = int(values[offset])
            distinct = int(values[offset + 1])
            min_v, max_v = values[offset + 2], values[offset + 3]
            offset += 4
            entry = {
                "name": name,
                "dtype": dtype,
                "null_count": nulls,
                "null_ratio": round(nulls / total, 6) if total else 0.0,
                "distinct_count": distinct,
                "min": _json_value(min_v),
                "max": _json_value(max_v),
            }
            if use_quantile:
                entry.update({
                    "p25": _json_value(values[offset]),
                    "p50": _json_value(values[offset + 1]),
                    "p75": _json_value(values[offset + 2]),
                })
                offset += 3
            if (sample_budget > 0 and "CHAR" in dtype.upper()
                    and 0 < distinct <= 50):
                sample_budget -= 1
                rows = conn.execute(
                    f"SELECT DISTINCT CAST({_ident(name)} AS VARCHAR) FROM {rel}"
                    f" WHERE {_ident(name)} IS NOT NULL LIMIT 5").fetchall()
                entry["sample_values"] = [r[0] for r in rows]
            profiles.append(entry)
        return profiles
