"""标的目录存储（M1）：SQLite 版本化导入 + 多条件检索。

写入纪律：
1. 同一 `sha256` 的快照重复导入直接返回既有结果（幂等），不重复写库；
2. 整批导入在单个事务内完成，异常整批回滚，避免半套目录；
3. 全部 SQL 参数化；LIKE 通配符由用户输入转义，防止语义污染。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.catalog.models import CatalogQuery
from backend.db import Database

ASSET_CLASSES = ("equity", "etf", "fund", "index", "fx", "crypto", "moneymarket")
ASSET_ALIASES = {
    "equities": "equity", "etfs": "etf", "funds": "fund", "indices": "index",
    "indexes": "index", "currencies": "fx", "currency": "fx", "cryptos": "crypto",
    "crypto": "crypto", "moneymarkets": "moneymarket", "moneymarket": "moneymarket",
}

_COLUMNS = (
    "id", "symbol", "exchange", "asset_class", "name", "currency", "sector",
    "industry", "country", "listing_status", "is_primary", "valid_from",
    "valid_to", "source", "source_version",
)

_INSERT = f"INSERT OR REPLACE INTO instruments({','.join(_COLUMNS)}) VALUES({','.join('?' * len(_COLUMNS))})"

_SELECT = (
    "SELECT id, symbol, exchange, asset_class, name, currency, sector, industry,"
    " country, listing_status, is_primary, valid_from, valid_to, source, source_version"
    " FROM instruments"
)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_asset_class(raw: str) -> str:
    key = str(raw or "").strip().lower()
    key = ASSET_ALIASES.get(key, key)
    return key if key in ASSET_CLASSES else "equity"


class CatalogStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 写入 ----------

    def import_records(self, records: Iterable[dict], *, source: str,
                       source_version: str, sha256: str = "", path: str = "",
                       replace: bool = True, as_of: str = "",
                       snapshot_id: str = "", batch_size: int = 1000,
                       dedupe: bool = True) -> dict:
        """导入一批标的（生成器流式消费，单事务）。

        返回 CatalogSnapshot 语义的 dict；同一 sha256 重复导入返回 idempotent=True。
        """
        if sha256 and dedupe:
            existing = self.db.query_one(
                "SELECT * FROM catalog_snapshots WHERE sha256=?", (sha256,))
            if existing:
                return {**self.snapshot_payload(existing), "idempotent": True}

        snapshot_id = snapshot_id or (
            f"cat_{sha256[:16]}" if sha256 else f"cat_{uuid.uuid4().hex[:16]}")
        imported_at = _now()
        valid_from = as_of or imported_at[:10]
        inserted = 0
        asset_counts: dict[str, int] = {}
        with self.db.tx() as conn:
            if replace:
                conn.execute("DELETE FROM instruments WHERE source=?", (source,))
            batch: list[tuple] = []
            for record in records:
                if hasattr(record, "model_dump"):
                    record = record.model_dump()
                row = self._row(record, source, source_version, valid_from)
                batch.append(row)
                asset_counts[row[3]] = asset_counts.get(row[3], 0) + 1
                if len(batch) >= batch_size:
                    conn.executemany(_INSERT, batch)
                    inserted += len(batch)
                    batch = []
            if batch:
                conn.executemany(_INSERT, batch)
                inserted += len(batch)
            if replace:
                conn.execute(
                    "DELETE FROM instrument_aliases WHERE instrument_id NOT IN"
                    " (SELECT id FROM instruments)")
            conn.execute(
                "INSERT OR REPLACE INTO catalog_snapshots"
                "(id, source, source_version, sha256, rows, path, imported_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (snapshot_id, source, source_version, sha256, inserted, path, imported_at))
        return {
            "snapshot_id": snapshot_id,
            "source": source,
            "source_version": source_version,
            "sha256": sha256,
            "rows": inserted,
            "asset_counts": asset_counts,
            "path": path,
            "imported_at": imported_at,
            "idempotent": False,
        }

    @staticmethod
    def _row(record: dict, source: str, source_version: str, valid_from: str) -> tuple:
        symbol = str(record.get("symbol") or "").strip()
        instrument_id = str(
            record.get("instrument_id") or record.get("id") or ""
        ).strip() or f"{record.get('exchange') or 'NA'}:{symbol}"
        status = record.get("listing_status")
        if not status:
            status = "delisted" if record.get("delisted") else "active"
        if status not in ("active", "delisted", "unknown"):
            status = "unknown"
        return (
            instrument_id,
            symbol,
            str(record.get("exchange") or ""),
            normalize_asset_class(str(record.get("asset_class") or "equity")),
            str(record.get("name") or ""),
            str(record.get("currency") or ""),
            str(record.get("sector") or ""),
            str(record.get("industry") or ""),
            str(record.get("country") or ""),
            status,
            1 if record.get("is_primary", True) else 0,
            str(record.get("valid_from") or valid_from),
            record.get("valid_to"),
            str(record.get("source") or source),
            str(record.get("source_version") or source_version),
        )

    # ---------- 检索 ----------

    def search(self, query: CatalogQuery | None = None, **kwargs: Any) -> dict:
        q = query or CatalogQuery(**kwargs)
        where, params = self._where(q)
        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS c FROM instruments {where}", params)
        order = "is_primary DESC, asset_class, symbol"
        order_params: list[Any] = []
        text = q.query.strip()
        if text:
            like = _escape_like(text)
            order = (
                "is_primary DESC, CASE"
                " WHEN lower(symbol)=lower(?) THEN 0"
                " WHEN lower(name)=lower(?) THEN 1"
                " WHEN symbol LIKE ? ESCAPE '\\' THEN 2"
                " WHEN name LIKE ? ESCAPE '\\' THEN 3"
                " ELSE 4 END, asset_class, symbol")
            order_params = [text, text, f"{like}%", f"{like}%"]
        rows = self.db.query(
            f"{_SELECT} {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, *order_params, q.limit, q.offset])
        return {
            "items": [self._item(r) for r in rows],
            "total": int(total_row["c"]) if total_row else 0,
            "limit": q.limit,
            "offset": q.offset,
        }

    def _where(self, q: CatalogQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if q.listing_status and q.listing_status != "all":
            clauses.append("listing_status=?")
            params.append(q.listing_status)
        if q.is_primary is not None:
            clauses.append("is_primary=?")
            params.append(1 if q.is_primary else 0)
        for field in ("symbol", "exchange", "country"):
            value = getattr(q, field).strip()
            if value:
                clauses.append(f"{field} LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(value)}%")
        if q.asset_class.strip():
            clauses.append("asset_class=?")
            params.append(normalize_asset_class(q.asset_class))
        for field in ("industry", "sector"):
            value = getattr(q, field).strip()
            if value:
                clauses.append(f"{field} LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(value)}%")
        text = q.query.strip()
        if text:
            like = f"%{_escape_like(text)}%"
            clauses.append(
                "(symbol LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"
                " OR industry LIKE ? ESCAPE '\\' OR sector LIKE ? ESCAPE '\\'"
                " OR id LIKE ? ESCAPE '\\'"
                " OR EXISTS(SELECT 1 FROM instrument_aliases a"
                " WHERE a.instrument_id=instruments.id"
                " AND a.alias LIKE ? ESCAPE '\\'))")
            params.extend([like] * 6)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def get(self, instrument_id: str) -> dict | None:
        row = self.db.query_one(f"{_SELECT} WHERE id=?", (instrument_id,))
        return self._item(row) if row else None

    def by_symbol(self, symbol: str, asset_class: str = "",
                  listing_status: str = "") -> list[dict]:
        clauses = ["symbol=?"]
        params: list[Any] = [symbol]
        if asset_class:
            clauses.append("asset_class=?")
            params.append(normalize_asset_class(asset_class))
        if listing_status and listing_status != "all":
            clauses.append("listing_status=?")
            params.append(listing_status)
        rows = self.db.query(f"{_SELECT} WHERE {' AND '.join(clauses)}", params)
        return [self._item(r) for r in rows]

    def aliases(self, instrument_id: str) -> list[str]:
        rows = self.db.query(
            "SELECT alias FROM instrument_aliases WHERE instrument_id=? ORDER BY alias",
            (instrument_id,))
        return [r["alias"] for r in rows]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM instruments")
        return int(row["c"]) if row else 0

    def stats(self) -> dict:
        total = self.count()
        by_asset = self.db.query(
            "SELECT asset_class, COUNT(*) AS count FROM instruments"
            " GROUP BY asset_class ORDER BY count DESC")
        by_exchange = self.db.query(
            "SELECT exchange, COUNT(*) AS count FROM instruments"
            " WHERE exchange != '' GROUP BY exchange ORDER BY count DESC LIMIT 10")
        by_country = self.db.query(
            "SELECT country, COUNT(*) AS count FROM instruments"
            " WHERE country != '' GROUP BY country ORDER BY count DESC LIMIT 10")
        status = self.db.query(
            "SELECT listing_status, COUNT(*) AS count FROM instruments"
            " GROUP BY listing_status")
        return {
            "total": total,
            "by_asset_class": by_asset,
            "by_exchange": by_exchange,
            "by_country": by_country,
            "by_status": status,
        }

    def snapshots(self, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM catalog_snapshots ORDER BY imported_at DESC, id DESC LIMIT ?",
            (limit,))
        return [self.snapshot_payload(r) for r in rows]

    @staticmethod
    def snapshot_payload(row: dict) -> dict:
        return {
            "snapshot_id": row["id"],
            "source": row.get("source", ""),
            "source_version": row.get("source_version", ""),
            "sha256": row.get("sha256", ""),
            "rows": int(row.get("rows") or 0),
            "path": row.get("path", ""),
            "imported_at": row.get("imported_at", ""),
        }

    @staticmethod
    def _item(row: dict) -> dict:
        return {
            "instrument_id": row["id"],
            "symbol": row["symbol"],
            "exchange": row["exchange"] or "",
            "asset_class": row["asset_class"] or "equity",
            "name": row["name"] or "",
            "currency": row["currency"] or "",
            "sector": row["sector"] or "",
            "industry": row["industry"] or "",
            "country": row["country"] or "",
            "listing_status": row["listing_status"] or "unknown",
            "is_primary": bool(row["is_primary"]),
            "valid_from": row["valid_from"] or "",
            "valid_to": row["valid_to"],
            "source": row["source"] or "",
            "source_version": row["source_version"] or "",
        }


def dump_snapshot_manifest(payload: dict, asset_counts: dict[str, int],
                           license_name: str, source_path: str) -> str:
    return json.dumps({
        **payload,
        "asset_counts": asset_counts,
        "license": license_name,
        "source_path": source_path,
    }, ensure_ascii=False, indent=2)
