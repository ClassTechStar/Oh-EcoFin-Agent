"""SQLite 连接与建表（WAL 模式，支撑快照线程与请求线程并发读写）。

全部 SQL 使用参数化占位符，无字符串拼接（安全纪律）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

#: 既有库的增量列迁移（新库走 DDL 全量建表；旧库 ALTER 补列）
#: 表/列名均为本文件内常量，非用户输入，允许 f-string 拼接。
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "agent_runs": {"self_critique": "TEXT"},
    "debate_runs": {"datapack": "TEXT"},
}

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  title TEXT,
  created_at TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  run_id TEXT,
  rating TEXT,
  comment TEXT,
  prompt_version TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  role TEXT,
  content TEXT,
  tool_call_id TEXT,
  tokens INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  name TEXT,
  args_json TEXT,
  result_json TEXT,
  ms INTEGER,
  ok INTEGER,
  cached INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  model TEXT,
  tier TEXT,
  role TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  cached_tokens INTEGER,
  cost_cents INTEGER,
  latency_ms INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT,
  verdict TEXT,
  thesis_digest TEXT,
  snapshot_path TEXT,
  data_as_of TEXT,
  horizon_days INTEGER,
  settled REAL,
  settled_at TEXT,
  reflection TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  session_id TEXT,
  event_id INTEGER,
  event TEXT,
  payload_json TEXT,
  created_at TEXT,
  PRIMARY KEY(session_id, event_id)
);
CREATE TABLE IF NOT EXISTS pending_asks(
  session_id TEXT PRIMARY KEY,
  qid TEXT,
  question TEXT,
  options_json TEXT,
  state TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS debate_runs(
  id TEXT PRIMARY KEY,
  symbol TEXT,
  as_of TEXT,
  context_id TEXT,
  status TEXT,
  verdict TEXT,
  datapack TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS debate_sections(
  run_id TEXT,
  role TEXT,
  round INTEGER,
  raw_text TEXT,
  model TEXT,
  signal REAL,
  strength REAL,
  created_at TEXT,
  PRIMARY KEY(run_id, role, round)
);
CREATE TABLE IF NOT EXISTS backtest_runs(
  id TEXT PRIMARY KEY,
  strategy_id TEXT,
  spec_hash TEXT,
  start_date TEXT,
  end_date TEXT,
  metrics_json TEXT,
  cost_model_json TEXT,
  skipped_json TEXT,
  data_asof TEXT,
  provider TEXT,
  warnings_json TEXT
);
CREATE TABLE IF NOT EXISTS leak_reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backtest_id TEXT,
  cut_date TEXT,
  passed INTEGER,
  trades_before_cut INTEGER,
  leaks_json TEXT,
  skipped_reason TEXT
);
CREATE TABLE IF NOT EXISTS alpha_signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_name TEXT,
  symbol TEXT,
  as_of TEXT,
  value REAL,
  confidence REAL,
  horizon_days INTEGER,
  abstained INTEGER,
  evidence_json TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT,
  as_of TEXT,
  weights_json TEXT,
  constraints_json TEXT,
  flat_reason TEXT
);
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY,
  strategy_id TEXT,
  symbol TEXT,
  side TEXT,
  qty INTEGER,
  price REAL,
  order_type TEXT,
  status TEXT,
  risk_decision_json TEXT,
  broker_order_id TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS risk_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  level TEXT,
  gate TEXT,
  rule TEXT,
  detail TEXT,
  symbol TEXT,
  order_id TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS promotions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT,
  spec_hash TEXT,
  backtest_id TEXT,
  approved INTEGER,
  gates_json TEXT,
  reasons_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,
  ref_id TEXT,
  payload_json TEXT,
  payload_hash TEXT,
  prev_hash TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS instruments(
  id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  exchange TEXT DEFAULT '',
  asset_class TEXT DEFAULT 'equity',
  name TEXT DEFAULT '',
  currency TEXT DEFAULT '',
  sector TEXT DEFAULT '',
  industry TEXT DEFAULT '',
  country TEXT DEFAULT '',
  listing_status TEXT DEFAULT 'active',
  is_primary INTEGER DEFAULT 1,
  valid_from TEXT DEFAULT '',
  valid_to TEXT,
  source TEXT DEFAULT '',
  source_version TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS instrument_aliases(
  instrument_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  alias_type TEXT DEFAULT 'name',
  PRIMARY KEY(instrument_id, alias, alias_type)
);
CREATE TABLE IF NOT EXISTS catalog_snapshots(
  id TEXT PRIMARY KEY,
  source TEXT,
  source_version TEXT,
  sha256 TEXT,
  rows INTEGER,
  path TEXT,
  imported_at TEXT
);
CREATE TABLE IF NOT EXISTS datasets(
  id TEXT PRIMARY KEY,
  workspace_id TEXT DEFAULT 'local',
  name TEXT,
  table_name TEXT,
  source TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS dataset_versions(
  id TEXT PRIMARY KEY,
  dataset_id TEXT,
  version INTEGER,
  sha256 TEXT,
  manifest_json TEXT,
  path TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS query_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_ids_json TEXT,
  sql_text TEXT,
  sql_hash TEXT,
  result_hash TEXT,
  rows INTEGER,
  elapsed_ms INTEGER,
  ok INTEGER,
  error TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS reports(
  id TEXT PRIMARY KEY,
  title TEXT,
  subtitle TEXT,
  status TEXT,
  spec_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS report_files(
  id TEXT PRIMARY KEY,
  report_id TEXT,
  format TEXT,
  filename TEXT,
  path TEXT,
  sha256 TEXT,
  bytes INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_runs(
  id TEXT PRIMARY KEY,
  session_id TEXT,
  mode TEXT,
  state TEXT,
  stop_reason TEXT,
  rounds INTEGER,
  tool_calls INTEGER,
  tool_errors INTEGER,
  tool_blocked INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER,
  cost_cents INTEGER,
  summary TEXT,
  self_critique TEXT,
  started_at TEXT,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence(
  id TEXT PRIMARY KEY,
  report_id TEXT,
  run_id TEXT,
  tool_call_id TEXT,
  block_id TEXT,
  source TEXT,
  locator TEXT,
  excerpt TEXT,
  value_hash TEXT,
  value_num REAL,
  value_text TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS number_claims(
  id TEXT PRIMARY KEY,
  report_id TEXT,
  block_id TEXT,
  claim TEXT,
  value REAL,
  unit TEXT,
  evidence_ids_json TEXT,
  passed INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT,
  level TEXT,
  gate TEXT,
  detail TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS sandbox_runs(
  id TEXT PRIMARY KEY,
  dataset_id TEXT,
  status TEXT,
  code_hash TEXT,
  exit_code INTEGER,
  duration_ms INTEGER,
  stdout TEXT,
  stderr TEXT,
  result_json TEXT,
  files_json TEXT,
  limits_json TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_toolcalls_session ON tool_calls(session_id, id);
CREATE INDEX IF NOT EXISTS idx_llmcalls_created ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_debate_sections_run ON debate_sections(run_id);
CREATE INDEX IF NOT EXISTS idx_leak_reports_backtest ON leak_reports(backtest_id);
CREATE INDEX IF NOT EXISTS idx_alpha_signals_symbol ON alpha_signals(symbol, as_of);
CREATE INDEX IF NOT EXISTS idx_promotions_strategy ON promotions(strategy_id, id);
CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(symbol);
CREATE INDEX IF NOT EXISTS idx_instruments_name ON instruments(name);
CREATE INDEX IF NOT EXISTS idx_instruments_asset ON instruments(asset_class, listing_status);
CREATE INDEX IF NOT EXISTS idx_instruments_industry ON instruments(industry);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON instrument_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_dataset_versions_dataset ON dataset_versions(dataset_id, version);
CREATE INDEX IF NOT EXISTS idx_query_audit_created ON query_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);
CREATE INDEX IF NOT EXISTS idx_report_files_report ON report_files(report_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_evidence_report ON evidence(report_id, block_id);
CREATE INDEX IF NOT EXISTS idx_claims_report ON number_claims(report_id, passed);
CREATE INDEX IF NOT EXISTS idx_audit_events_report ON audit_events(report_id, id);
CREATE INDEX IF NOT EXISTS idx_sandbox_runs_created ON sandbox_runs(created_at);
"""


def _now() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """线程安全 SQLite 封装：短事务 + WAL + 参数化查询。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._conn(path)
        conn.executescript(DDL)
        self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """轻量列迁移：已有库补齐新增列（幂等，缺列才 ALTER）。"""
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.commit()

    def _conn(self, path: str | Path | None = None) -> sqlite3.Connection:
        target = str(path or self.path)
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(self._local, "conn_path", None) != target:
            conn = sqlite3.connect(target, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            self._local.conn_path = target
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """短事务上下文：退出即提交，异常回滚。"""
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- 便捷方法（全部参数化） ----

    def insert_message(self, session_id: str, role: str, content: str,
                       tool_call_id: str | None = None, tokens: int = 0) -> int:
        return self.execute(
            "INSERT INTO messages(session_id, role, content, tool_call_id, tokens, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (session_id, role, content, tool_call_id, tokens, _now()),
        )

    def insert_tool_call(self, session_id: str, name: str, args: dict,
                         result: Any, ms: int, ok: bool, cached: bool) -> int:
        return self.execute(
            "INSERT INTO tool_calls(session_id, name, args_json, result_json, ms, ok, cached, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (session_id, name, json.dumps(args, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False, default=str), ms, int(ok), int(cached), _now()),
        )

    def insert_llm_call(self, session_id: str, model: str, tier: str, role: str,
                        tokens_in: int, tokens_out: int, cached_tokens: int,
                        cost_cents: int, latency_ms: int) -> int:
        return self.execute(
            "INSERT INTO llm_calls(session_id, model, tier, role, tokens_in, tokens_out,"
            " cached_tokens, cost_cents, latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_id, model, tier, role, tokens_in, tokens_out, cached_tokens,
             cost_cents, latency_ms, _now()),
        )

    def append_event(self, session_id: str, event_id: int, event: str, payload: dict) -> None:
        self.execute(
            "INSERT OR REPLACE INTO events(session_id, event_id, event, payload_json, created_at)"
            " VALUES(?,?,?,?,?)",
            (session_id, event_id, event, json.dumps(payload, ensure_ascii=False, default=str), _now()),
        )

    def load_events_after(self, session_id: str, last_event_id: int) -> list[dict]:
        rows = self.query(
            "SELECT event_id, event, payload_json FROM events"
            " WHERE session_id=? AND event_id>? ORDER BY event_id ASC",
            (session_id, last_event_id),
        )
        out = []
        for r in rows:
            try:
                out.append({"id": r["event_id"], "event": r["event"],
                            "payload": json.loads(r["payload_json"])})
            except json.JSONDecodeError:
                continue
        return out

    def cost_today_cents(self) -> int:
        today = _now()[:10]
        row = self.query_one(
            "SELECT COALESCE(SUM(cost_cents),0) AS c FROM llm_calls WHERE created_at LIKE ?",
            (f"{today}%",),
        )
        return int(row["c"]) if row else 0

    def export_cost_breakdown(self) -> list[dict]:
        return self.query(
            "SELECT model, tier, COUNT(*) AS calls, SUM(tokens_in) AS tokens_in,"
            " SUM(tokens_out) AS tokens_out, SUM(cached_tokens) AS cached_tokens,"
            " SUM(cost_cents) AS cost_cents FROM llm_calls GROUP BY model, tier"
        )


# ---- 异步桥接：把同步 DB 调用丢进线程池，避免阻塞事件循环 ----

class AsyncDatabase:
    def __init__(self, db: Database):
        self.db = db
        self._lock = asyncio.Lock()

    async def run(self, fn, *args, **kwargs):
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def insert_message(self, *a, **kw) -> int:
        return await self.run(self.db.insert_message, *a, **kw)

    async def insert_tool_call(self, *a, **kw) -> int:
        return await self.run(self.db.insert_tool_call, *a, **kw)

    async def insert_llm_call(self, *a, **kw) -> int:
        return await self.run(self.db.insert_llm_call, *a, **kw)

    async def append_event(self, *a, **kw) -> None:
        return await self.run(self.db.append_event, *a, **kw)

    async def load_events_after(self, session_id: str, last_id: int) -> list[dict]:
        return await self.run(self.db.load_events_after, session_id, last_id)

    async def cost_today_cents(self) -> int:
        return await self.run(self.db.cost_today_cents)
