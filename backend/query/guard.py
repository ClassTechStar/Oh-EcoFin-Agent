"""SQL Guard（M3）：只允许单条 SELECT/WITH 只读查询。

三层防线：
1. 本模块：语句白名单 + 危险关键字/文件函数拒绝 + 多语句拒绝；
2. DuckDB 连接：`read_only=True`，DDL/DML 在内核层直接失败；
3. `enable_external_access=false`：查询期禁止任何文件/网络表函数。
"""

from __future__ import annotations

import re

MAX_SQL_LENGTH = 20000

#: 会改变数据库/文件/会话状态或写数据的语句关键字
DANGEROUS_KEYWORDS: tuple[str, ...] = (
    "ATTACH", "DETACH", "COPY", "INSTALL", "LOAD", "PRAGMA", "CALL",
    "EXPORT", "IMPORT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP",
    "ALTER", "TRUNCATE", "MERGE", "GRANT", "REVOKE", "VACUUM", "CHECKPOINT",
    "SET", "RESET", "USE", "BEGIN", "COMMIT", "ROLLBACK", "TRANSACTION",
    "ANALYZE", "EXPLAIN", "INTO", "REPLACE", "UPSERT", "PREPARE",
    "EXECUTE", "DEALLOCATE", "COMMENT", "DESCRIBE", "SUMMARIZE", "SHOW",
)

#: 可读任意文件/网络/其他数据库的表函数
BLOCKED_FUNCTIONS: tuple[str, ...] = (
    "read_csv", "read_csv_auto", "read_parquet", "read_json",
    "read_json_auto", "read_ndjson", "read_ndjson_auto", "read_xlsx",
    "parquet_scan", "csv_scan", "json_scan", "glob", "sqlite_scan",
    "sqlite_attach", "postgres_scan", "postgres_attach", "mysql_scan",
    "mysql_attach", "delta_scan", "iceberg_scan", "httpfs", "read_blob",
    "read_text", "sniff_csv", "duckdb_settings", "which_secret",
    "st_read", "st_readosm",
)

_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(DANGEROUS_KEYWORDS) + r")\b", re.IGNORECASE)
_FUNCTION_RE = re.compile(
    r"\b(" + "|".join(BLOCKED_FUNCTIONS) + r")\s*\(", re.IGNORECASE)
_READ_ONLY_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


class SqlGuardError(ValueError):
    def __init__(self, message: str, *, code: str = "blocked", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def mask_literals_and_comments(sql: str) -> str:
    """把字符串/标识符/注释替换为空格（保留换行），供关键字扫描使用。"""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.extend((" ", " "))
                        i += 2
                        continue
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            out.extend((" ", " "))
            i += 2
            while i < n:
                if sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    out.extend((" ", " "))
                    i += 2
                    break
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def validate_sql(sql: str) -> str:
    """校验并返回可执行 SQL（保留原始大小写与字面量）。"""
    if not isinstance(sql, str):
        raise SqlGuardError("SQL 必须是字符串", code="invalid")
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlGuardError("SQL 不能为空", code="invalid")
    if len(text) > MAX_SQL_LENGTH:
        raise SqlGuardError(
            f"SQL 过长: {len(text)} > {MAX_SQL_LENGTH}", code="invalid",
            hint="请拆分为多条查询或减少字面量")
    masked = mask_literals_and_comments(text)
    if "$" in masked:
        raise SqlGuardError(
            "不允许美元符引用/参数占位", code="blocked",
            hint="使用普通 SQL 字面量，避免 $tag$ 语法")
    body = masked.strip()
    if ";" in body:
        raise SqlGuardError(
            "只允许单条只读语句", code="multiple_statements",
            hint="一次只提交一条 SELECT/WITH 查询")
    if not _READ_ONLY_START.match(body):
        raise SqlGuardError(
            "只允许 SELECT / WITH 查询", code="not_read_only",
            hint="DDL/DML/管理语句一律拒绝")
    keyword = _KEYWORD_RE.search(body)
    if keyword:
        raise SqlGuardError(
            f"不允许的关键字: {keyword.group(1).upper()}", code="blocked",
            hint="查询只能读取数据，不能修改数据库或会话状态")
    function = _FUNCTION_RE.search(body)
    if function:
        raise SqlGuardError(
            f"不允许的文件/外部访问函数: {function.group(1)}", code="blocked",
            hint="只能查询已登记的数据集表")
    if not re.search(r"\bSELECT\b", body, re.IGNORECASE):
        raise SqlGuardError("WITH 查询必须包含 SELECT", code="invalid")
    return text
