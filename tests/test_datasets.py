"""M2 数据集导入：Parquet 快照、manifest、限额与画像。"""

from __future__ import annotations

import hashlib
import io
import json

import duckdb
import pandas as pd
import pytest

from backend.config import Settings
from backend.datasets.models import (
    DatasetImportError,
    DatasetLimitError,
    DatasetNotFoundError,
)
from backend.datasets.service import DatasetService

CSV_TEXT = "date,region,amount,qty\n2026-01-01,North,100.5,2\n2026-01-02,South,,3\n2026-01-03,North,50,1\n"


@pytest.fixture()
def service(tmp_path, tmp_db):
    s = Settings(db_path=str(tmp_path / "test.db"))
    s.datasets.dir = str(tmp_path / "datasets")
    s.datasets.workspace_dir = str(tmp_path / "workspaces")
    return DatasetService(tmp_db, s), s


def _csv(tmp_path, text: str = CSV_TEXT, name: str = "sales.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_import_csv_snapshot_and_manifest(service, tmp_path):
    svc, _ = service
    result = svc.import_path(_csv(tmp_path))
    assert result["rows"] == 3
    assert result["columns"] == 4
    assert result["version"] == 1
    assert result["table_name"] == "sales"
    assert result["created"] is True

    parquet = tmp_path / "datasets" / result["sha256"][:2] / result["sha256"][2:4] \
        / result["sha256"] / "data.parquet"
    assert parquet.is_file()
    assert hashlib.sha256(parquet.read_bytes()).hexdigest() == result["sha256"]

    manifest = json.loads((parquet.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == result["sha256"]
    assert manifest["rows"] == 3
    assert manifest["format"] == "parquet"
    assert manifest["schema_json"]["region"] == "VARCHAR"

    again = svc.import_path(_csv(tmp_path))
    assert again["idempotent"] is True
    assert again["dataset_id"] == result["dataset_id"]
    assert len(svc.store.versions(result["dataset_id"])) == 1


def test_new_content_bumps_version(service, tmp_path):
    svc, _ = service
    first = svc.import_path(_csv(tmp_path))
    updated = _csv(tmp_path, CSV_TEXT + "2026-01-04,West,80,5\n")
    second = svc.import_path(updated)
    assert second["dataset_id"] == first["dataset_id"]
    assert second["version"] == 2
    assert second["rows"] == 4
    assert second["table_name"] == first["table_name"]

    items = svc.list()
    assert len(items) == 1
    assert items[0]["version"] == 2
    assert items[0]["rows"] == 4
    assert len(svc.store.versions(first["dataset_id"])) == 2


def test_limits(service, tmp_path):
    svc, settings = service
    settings.datasets.max_rows = 2
    with pytest.raises(DatasetLimitError):
        svc.import_path(_csv(tmp_path, name="rows.csv"))

    settings.datasets.max_rows = 100
    settings.datasets.max_columns = 2
    with pytest.raises(DatasetLimitError):
        svc.import_path(_csv(tmp_path, name="cols.csv"))

    settings.datasets.max_columns = 100
    svc.max_bytes = 20
    with pytest.raises(DatasetLimitError):
        svc.import_path(_csv(tmp_path, name="big.csv"))


def test_excel_and_gbk_csv(service, tmp_path):
    svc, _ = service
    xlsx = tmp_path / "book.xlsx"
    pd.DataFrame({"日期": ["2026-01-01", "2026-01-02"], "金额": [10.5, 20.0]}).to_excel(
        xlsx, index=False)
    xlsx_result = svc.import_path(xlsx)
    assert xlsx_result["rows"] == 2
    assert set(xlsx_result["manifest"]["schema_json"]) == {"日期", "金额"}

    gbk = tmp_path / "cn.csv"
    gbk.write_text("地区,金额\n华东,10\n华南,20\n", encoding="gbk")
    gbk_result = svc.import_path(gbk)
    assert gbk_result["rows"] == 2
    assert "地区" in gbk_result["manifest"]["schema_json"]

    bom = tmp_path / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbf" + "地区,金额\n华东,10\n".encode())
    bom_result = svc.import_path(bom)
    assert bom_result["rows"] == 1
    assert "地区" in bom_result["manifest"]["schema_json"]


def test_parquet_import(service, tmp_path):
    svc, _ = service
    parquet = tmp_path / "source.parquet"
    conn = duckdb.connect()
    try:
        conn.execute(
            f"COPY (SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2, 'y')"
            f" TO '{str(parquet).replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)")
    finally:
        conn.close()
    result = svc.import_path(parquet, name="parquet_ds")
    assert result["rows"] == 2
    assert result["manifest"]["schema_json"] == {"a": "INTEGER", "b": "VARCHAR"}


def test_profile(service, tmp_path):
    svc, _ = service
    result = svc.import_path(_csv(tmp_path))
    profile = svc.profile(result["dataset_id"])
    assert profile["rows"] == 3
    assert profile["truncated_columns"] == 0
    cols = {c["name"]: c for c in profile["profile"]}
    assert cols["amount"]["null_count"] == 1
    assert cols["amount"]["null_ratio"] == pytest.approx(1 / 3, abs=1e-5)
    assert cols["region"]["distinct_count"] == 2
    assert cols["amount"]["p50"] is not None
    assert "North" in cols["region"]["sample_values"]
    assert len(profile["sample"]) == 3
    assert profile["schema"]["qty"] == "BIGINT"


def test_profile_missing_dataset(service):
    svc, _ = service
    with pytest.raises(DatasetNotFoundError):
        svc.profile("ds_missing")


def test_csv_validation_rejects_malformed_rows(service, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b,c\n1,2,3\n4,5\n", encoding="utf-8")
    with pytest.raises(DatasetImportError):
        service[0].import_path(bad)

    duplicate = tmp_path / "dup.csv"
    duplicate.write_text("a,a\n1,2\n", encoding="utf-8")
    with pytest.raises(DatasetImportError):
        service[0].import_path(duplicate)

    # 空字段 + CRLF（DuckDB 嗅探易误判）必须正常导入且行数一致
    crlf = tmp_path / "crlf.csv"
    crlf.write_bytes(
        b"date,amount,stage\r\n2026-01-01,100,visit\r\n"
        b"2026-01-02,,cart\r\n2026-01-03,50,pay\r\n")
    result = service[0].import_path(crlf)
    assert result["rows"] == 3


def test_import_upload_and_rejects(service):
    svc, _ = service
    result = svc.import_upload(io.BytesIO(CSV_TEXT.encode("utf-8")), "upload.csv")
    assert result["rows"] == 3
    assert result["name"] == "upload"

    with pytest.raises(DatasetImportError):
        svc.import_upload(io.BytesIO(b"abc"), "notes.txt")
    with pytest.raises(DatasetImportError):
        svc.import_upload(io.BytesIO(b"abc"), "old.xls")
    with pytest.raises(DatasetImportError):
        svc.import_path("no_such_file.csv")
