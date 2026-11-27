"""M3 查询安全：SQL Guard 拒绝矩阵 + DuckDB 只读执行/限额/审计。"""

from __future__ import annotations

import pytest

from backend.config import Settings
from backend.datasets.service import DatasetService
from backend.query.engine import (
    QueryAuditStore,
    QueryEngine,
    QueryExecutionError,
    QueryTimeoutError,
)
from backend.query.guard import SqlGuardError, mask_literals_and_comments, validate_sql

ROWS = ["id,region,amount"] + [
    f"{i},{'North' if i % 2 else 'South'},{i * 10}.5" for i in range(1, 11)
]
CSV_TEXT = "\n".join(ROWS) + "\n"


@pytest.mark.parametrize("sql,code", [
    ("DROP TABLE trades", "not_read_only"),
    ("DELETE FROM trades", "not_read_only"),
    ("UPDATE trades SET amount=1", "not_read_only"),
    ("INSERT INTO trades VALUES(1)", "not_read_only"),
    ("ALTER TABLE trades ADD COLUMN x INT", "not_read_only"),
    ("CREATE TABLE x(a INT)", "not_read_only"),
    ("ATTACH 'x.db' AS x", "not_read_only"),
    ("PRAGMA version", "not_read_only"),
    ("COPY trades TO 'x.csv'", "not_read_only"),
    ("SET threads=1", "not_read_only"),
    ("SHOW TABLES", "not_read_only"),
    ("EXPLAIN SELECT * FROM trades", "not_read_only"),
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("SELECT * FROM read_csv_auto('x.csv')", "blocked"),
    ("SELECT * FROM read_parquet('x.parquet')", "blocked"),
    ("SELECT * FROM glob('*')", "blocked"),
    ("SELECT 1 INTO backup", "blocked"),
    ("WITH x AS (SELECT 1) INSERT INTO trades SELECT * FROM x", "blocked"),
    ("SELECT $$hide$$", "blocked"),
    ("", "invalid"),
])
def test_guard_rejects(sql, code):
    with pytest.raises(SqlGuardError) as exc:
        validate_sql(sql)
    assert exc.value.code == code
    assert exc.value.hint or code == "invalid"


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT 1;",
    "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
    "SELECT 'DROP TABLE trades' AS note",
    "SELECT 1 AS a -- DROP TABLE trades",
    'SELECT "update" FROM trades',
    "SELECT amount / 2 AS half FROM trades WHERE region = 'North'",
])
def test_guard_allows_read_only(sql):
    assert validate_sql(sql)


def test_mask_hides_literals_but_keeps_structure():
    masked = mask_literals_and_comments("SELECT 'DELETE' -- DROP\nFROM t")
    assert "DELETE" not in masked
    assert "DROP" not in masked
    assert "SELECT" in masked and "FROM t" in masked


@pytest.fixture()
def engine(tmp_path, tmp_db):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    service = DatasetService(tmp_db, settings)
    csv = tmp_path / "trades.csv"
    csv.write_text(CSV_TEXT, encoding="utf-8")
    dataset = service.import_path(csv)
    eng = QueryEngine(service, QueryAuditStore(tmp_db),
                      settings.datasets.workspace_dir,
                      max_rows=100, timeout_seconds=5)
    return eng, dataset, tmp_db


def test_query_aggregate_and_audit(engine):
    eng, dataset, db = engine
    result = eng.execute(
        f"SELECT region, SUM(amount) AS total FROM {dataset['table_name']}"
        " GROUP BY region ORDER BY total DESC")
    assert result["ok"] is True
    assert result["columns"] == ["region", "total"]
    assert result["rows"][0][0] == "South"
    assert result["rows"][0][1] == pytest.approx(302.5)
    assert result["result_hash"] and result["sql_hash"]
    assert result["datasets"] == [dataset["dataset_id"]]

    audit = QueryAuditStore(db).recent(5)
    assert audit[0]["ok"] is True
    assert audit[0]["rows"] == 2
    assert audit[0]["dataset_ids"] == [dataset["dataset_id"]]


def test_cte_query(engine):
    eng, dataset, _ = engine
    result = eng.execute(
        f"WITH north AS (SELECT * FROM {dataset['table_name']} WHERE region='North')"
        " SELECT COUNT(*) AS n, SUM(amount) AS total FROM north")
    assert result["rows"] == [[5, pytest.approx(252.5)]]


def test_row_limit_truncates(engine):
    eng, dataset, _ = engine
    result = eng.execute(f"SELECT * FROM {dataset['table_name']}", max_rows=3)
    assert result["row_count"] == 3
    assert result["truncated"] is True
    assert len(result["rows"]) == 3


def test_unknown_table_has_hint(engine):
    eng, dataset, _ = engine
    with pytest.raises(QueryExecutionError) as exc:
        eng.execute("SELECT * FROM not_a_table")
    assert "not_a_table" in str(exc.value)
    assert dataset["table_name"] in exc.value.hint


def test_guard_failure_is_audited(engine):
    eng, _, db = engine
    with pytest.raises(SqlGuardError):
        eng.execute("DROP TABLE trades")
    audit = QueryAuditStore(db).recent(1)[0]
    assert audit["ok"] is False
    assert "not_read_only" in audit["error"]


def test_dataset_filter_limits_visible_tables(engine):
    eng, dataset, _ = engine
    result = eng.execute(f"SELECT COUNT(*) FROM {dataset['table_name']}",
                         dataset_ids=[dataset["dataset_id"]])
    assert result["rows"] == [[10]]
    with pytest.raises(QueryExecutionError):
        eng.execute("SELECT 1", dataset_ids=["ds_missing"])


def test_timeout_interrupts(tmp_path, tmp_db):
    settings = Settings(db_path=str(tmp_path / "test.db"))
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    service = DatasetService(tmp_db, settings)
    csv = tmp_path / "tiny.csv"
    csv.write_text("a\n1\n", encoding="utf-8")
    service.import_path(csv)
    eng = QueryEngine(service, QueryAuditStore(tmp_db),
                      settings.datasets.workspace_dir,
                      max_rows=10, timeout_seconds=0.4)
    with pytest.raises(QueryTimeoutError):
        eng.execute("SELECT SUM(sin(range)) FROM range(5000000000)")
    assert QueryAuditStore(tmp_db).recent(1)[0]["ok"] is False
