"""FinanceDatabase 快照导入（M1）。

源项目：FinanceDatabase（MIT）。只导入“存在哪些标的”的目录字段，不导入行情。
支持三种落盘形态：`compression/*.parquet`（首选）、`compression/*.bz2`、
`database/*.csv`；优先使用 parquet，避免 300k 行 CSV 解析开销。

来源与许可随快照落盘（manifest.json），同一内容哈希重复导入幂等。
"""

from __future__ import annotations

import bz2
import hashlib
import shutil
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from backend.catalog.models import CatalogError
from backend.catalog.store import CatalogStore, dump_snapshot_manifest

SOURCE = "financedatabase"
LICENSE = "MIT"
SOURCE_URL = "https://github.com/JerBouma/FinanceDatabase"

#: FinanceDatabase 资产文件 → 内部资产类别
ASSET_FILES: dict[str, str] = {
    "equities": "equity",
    "etfs": "etf",
    "funds": "fund",
    "indices": "index",
    "currencies": "fx",
    "cryptos": "crypto",
    "moneymarkets": "moneymarket",
}

_SUFFIX_VENUE = {
    ".SS": "CN", ".SZ": "CN", ".BJ": "CN", ".HK": "HK", ".T": "JP", ".L": "UK",
    ".AX": "AU", ".TO": "CA", ".V": "CA", ".DE": "DE", ".PA": "FR", ".AS": "NL",
    ".SW": "CH", ".MI": "IT", ".MC": "ES", ".ST": "SE", ".OL": "NO", ".CO": "DK",
    ".HE": "FI", ".SI": "SG", ".KS": "KR", ".KQ": "KR", ".TW": "TW", ".NS": "IN",
    ".BO": "IN", ".SA": "BR", ".MX": "MX", ".NZ": "NZ", ".IR": "IE", ".IS": "TR",
}

_COUNTRY_VENUE = {
    "china": "CN", "hong kong": "HK", "united states": "US", "japan": "JP",
    "united kingdom": "UK", "canada": "CA", "australia": "AU", "germany": "DE",
    "france": "FR", "netherlands": "NL", "switzerland": "CH", "italy": "IT",
    "spain": "ES", "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "singapore": "SG", "south korea": "KR", "taiwan": "TW", "india": "IN",
    "brazil": "BR", "mexico": "MX", "new zealand": "NZ", "ireland": "IE",
    "turkey": "TR",
}

_EXCHANGE_VENUE = {
    "SHH": "CN", "SHZ": "CN", "BJS": "CN", "HKG": "HK",
    "NMS": "US", "NYQ": "US", "SNP": "US", "ASE": "US", "PCX": "US", "BAT": "US",
    "LSE": "UK", "TSE": "JP", "ASX": "AU", "TSX": "CA", "TSX-V": "CA",
    "FRA": "DE", "GER": "DE", "PAR": "FR", "AMS": "NL", "SWX": "CH", "MIL": "IT",
    "MCE": "ES", "STO": "SE", "OSL": "NO", "CPH": "DK", "HEL": "FI",
    "SES": "SG", "KSC": "KR", "KSE": "KR", "TAI": "TW", "NSE": "IN", "BSE": "IN",
    "SAO": "BR", "BMV": "MX", "NZE": "NZ",
}


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def venue_code(symbol: str, exchange: str = "", country: str = "") -> str:
    """稳定 venue 码：symbol 后缀 → 交易所 → 国家，最后回退 NA。"""
    upper = symbol.upper()
    for suffix, venue in _SUFFIX_VENUE.items():
        if upper.endswith(suffix):
            return venue
    code = _EXCHANGE_VENUE.get(exchange.upper())
    if code:
        return code
    code = _COUNTRY_VENUE.get(country.strip().lower())
    return code or (exchange.upper() or "NA")


def instrument_id_for(symbol: str, exchange: str = "", country: str = "") -> str:
    """内部稳定 ID（方案 6.6 示例 `CN:600519`）。"""
    return f"{venue_code(symbol, exchange, country)}:{symbol.strip()}"


def _escaped(path: Path) -> str:
    return str(path).replace("'", "''")


def _sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _detect_layout(root: Path) -> tuple[Path, dict[str, tuple[Path, str]]]:
    """在 root 下寻找可导入的资产文件，返回 (基准目录, {asset: (path, ext)})。

    compression/ 优先于 database/，逐资产回退，避免只导入半套目录。
    """
    candidates = [base for base in (root / "compression", root / "database", root)
                  if base.is_dir()]
    found: dict[str, tuple[Path, str]] = {}
    for asset in ASSET_FILES:
        for base in candidates:
            for ext in (".parquet", ".bz2", ".csv"):
                candidate = base / f"{asset}{ext}"
                if candidate.is_file():
                    found[asset] = (candidate, ext)
                    break
            if asset in found:
                break
    if not found:
        raise CatalogError(
            f"未找到 FinanceDatabase 数据文件: {root}",
            hint="请指向 FinanceDatabase 仓库根目录（含 compression/ 或 database/）",
        )
    return root, found


def _snapshot_digest(files: dict[str, tuple[Path, str]]) -> str:
    h = hashlib.sha256()
    for asset, (path, _ext) in sorted(files.items()):
        h.update(asset.encode("utf-8"))
        h.update(_sha256_file(path).encode("utf-8"))
    return h.hexdigest()


def _decode_csv(raw: Path, cleanup: list[Path]) -> Path:
    """UTF-8 直读；剥离 BOM；非 UTF-8（如 GBK）转码，避免静默乱码。"""
    data = raw.read_bytes()
    if not data:
        return raw
    if data.startswith(b"\xef\xbb\xbf"):
        tmp = Path(tempfile.NamedTemporaryFile(
            delete=False, suffix=".utf8.csv", prefix="catalog_").name)
        tmp.write_bytes(data[3:])
        cleanup.append(tmp)
        return tmp
    try:
        data[:65536].decode("utf-8")
        return raw
    except UnicodeDecodeError:
        pass
    text = data.decode("gbk", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    tmp = Path(tempfile.NamedTemporaryFile(
        delete=False, suffix=".utf8.csv", prefix="catalog_").name)
    tmp.write_text(text, encoding="utf-8")
    cleanup.append(tmp)
    return tmp


def _iter_asset(conn: duckdb.DuckDBPyConnection, path: Path, ext: str,
                asset: str, as_of: str) -> Iterator[dict]:
    """流式读取单个资产文件并映射为目录记录。"""
    cleanup: list[Path] = []
    try:
        if ext == ".parquet":
            rel = f"read_parquet('{_escaped(path)}')"
        elif ext == ".bz2":
            tmp = Path(tempfile.NamedTemporaryFile(
                delete=False, suffix=".csv", prefix="catalog_").name)
            cleanup.append(tmp)
            with bz2.open(path, "rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            rel = f"read_csv_auto('{_escaped(_decode_csv(tmp, cleanup))}', header=true)"
        else:
            rel = f"read_csv_auto('{_escaped(_decode_csv(path, cleanup))}', header=true)"
        cursor = conn.execute(f"SELECT * FROM {rel}")
        names = [d[0] for d in cursor.description]
        asset_class = ASSET_FILES[asset]
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for values in batch:
                row = dict(zip(names, values, strict=False))
                yield _map_row(row, asset, asset_class, as_of)
    finally:
        for tmp in cleanup:
            tmp.unlink(missing_ok=True)


def _map_row(row: dict, asset: str, asset_class: str, as_of: str) -> dict:
    symbol = _text(row.get("symbol"))
    exchange = _text(row.get("exchange"))
    country = _text(row.get("country"))
    name = _text(row.get("name"))
    sector = ""
    industry = ""
    if asset == "equities":
        sector, industry = _text(row.get("sector")), _text(row.get("industry"))
    elif asset in ("etfs", "funds", "indices"):
        sector, industry = _text(row.get("category_group")), _text(row.get("category"))
    elif asset == "cryptos":
        sector = "Cryptocurrency"
        industry = _text(row.get("cryptocurrency"))
    elif asset == "moneymarkets":
        sector = "Money Market"
        industry = _text(row.get("family"))
    elif asset == "currencies":
        sector = "Foreign Exchange"
    delisted = bool(row.get("delisted")) if "delisted" in row else False
    return {
        "instrument_id": instrument_id_for(symbol, exchange, country),
        "symbol": symbol,
        "exchange": exchange,
        "asset_class": asset_class,
        "name": name,
        "currency": _text(row.get("currency")),
        "sector": sector,
        "industry": industry,
        "country": country,
        "listing_status": "delisted" if delisted else "active",
        "is_primary": True,
        "valid_from": as_of,
        "valid_to": None,
        "source": SOURCE,
        "source_version": "",
    }


def import_financedatabase(root: str | Path, store: CatalogStore,
                           catalog_dir: str | Path, *,
                           as_of: str = "", source_version: str = "",
                           force: bool = False) -> dict:
    """导入 FinanceDatabase 目录快照（幂等）。返回导入摘要。"""
    root = Path(root).expanduser().resolve()
    if not root.exists():
        raise CatalogError(f"目录不存在: {root}")
    base, files = _detect_layout(root)
    digest = _snapshot_digest(files)
    as_of = as_of or _now()[:10]
    version = source_version or f"financedatabase-{as_of}"
    snapshot_id = f"cat_{digest[:16]}"
    snapshot_dir = Path(catalog_dir).expanduser().resolve() / snapshot_id
    manifest_path = snapshot_dir / "manifest.json"

    if not force:
        existing = store.db.query_one(
            "SELECT * FROM catalog_snapshots WHERE sha256=?", (digest,))
        if existing:
            return {**store.snapshot_payload(existing), "idempotent": True}

    conn = duckdb.connect()
    try:
        def records() -> Iterator[dict]:
            for asset in ASSET_FILES:
                if asset not in files:
                    continue
                path, ext = files[asset]
                for rec in _iter_asset(conn, path, ext, asset, as_of):
                    rec["source_version"] = version
                    yield rec

        result = store.import_records(
            records(), source=SOURCE, source_version=version, sha256=digest,
            path=str(manifest_path), as_of=as_of, replace=True,
            snapshot_id=snapshot_id, dedupe=not force)
    finally:
        conn.close()

    if result.get("idempotent"):
        return result

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        dump_snapshot_manifest(result, result.get("asset_counts", {}),
                               LICENSE, str(base)), encoding="utf-8")
    result["path"] = str(manifest_path)
    result["source_url"] = SOURCE_URL
    result["license"] = LICENSE
    # 资产类别计数不落库，随 manifest 文件保留
    result.pop("asset_counts", None)
    return result
