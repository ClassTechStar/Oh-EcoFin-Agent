"""M1 标的目录：版本化导入、检索、幂等与 FinanceDatabase 适配。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.catalog.importer import (
    import_financedatabase,
    instrument_id_for,
    venue_code,
)
from backend.catalog.models import CatalogQuery
from backend.catalog.store import CatalogStore


def _records() -> list[dict]:
    return [
        {
            "instrument_id": "CN:600519", "symbol": "600519.SS", "exchange": "SHH",
            "asset_class": "equity", "name": "Kweichow Moutai", "currency": "CNY",
            "sector": "Consumer Staples", "industry": "Beverages",
            "country": "China", "listing_status": "active", "is_primary": True,
        },
        {
            "instrument_id": "CN:000001", "symbol": "000001.SZ", "exchange": "SHZ",
            "asset_class": "equity", "name": "Ping An Bank", "currency": "CNY",
            "sector": "Financials", "industry": "Banks",
            "country": "China", "listing_status": "active", "is_primary": True,
        },
        {
            "instrument_id": "US:SPY", "symbol": "SPY", "exchange": "PCX",
            "asset_class": "etf", "name": "SPDR S&P 500 ETF Trust", "currency": "USD",
            "sector": "Equity", "industry": "Large Blend",
            "country": "United States", "listing_status": "active", "is_primary": True,
        },
        {
            "instrument_id": "US:OLD", "symbol": "OLD", "exchange": "NMS",
            "asset_class": "equity", "name": "Old Corp", "currency": "USD",
            "industry": "Banks", "country": "United States",
            "listing_status": "delisted", "is_primary": False,
        },
    ]


def test_import_search_and_stats(tmp_db):
    store = CatalogStore(tmp_db)
    result = store.import_records(
        _records(), source="fixture", source_version="v1", sha256="a" * 64)
    assert result["rows"] == 4
    assert result["idempotent"] is False
    assert store.count() == 4

    hit = store.search(CatalogQuery(query="moutai"))
    assert hit["total"] == 1
    assert hit["items"][0]["instrument_id"] == "CN:600519"

    exact = store.search(CatalogQuery(query="000001.SZ"))
    assert exact["items"][0]["instrument_id"] == "CN:000001"

    banks = store.search(CatalogQuery(industry="banks"))
    assert banks["total"] == 1
    assert banks["items"][0]["symbol"] == "000001.SZ"
    all_banks = store.search(CatalogQuery(industry="banks", listing_status="all"))
    assert {i["symbol"] for i in all_banks["items"]} == {"000001.SZ", "OLD"}

    etfs = store.search(CatalogQuery(asset_class="etfs"))
    assert etfs["total"] == 1 and etfs["items"][0]["asset_class"] == "etf"

    stats = store.stats()
    assert stats["total"] == 4
    assert {r["asset_class"]: r["count"] for r in stats["by_asset_class"]} == {
        "equity": 3, "etf": 1}


def test_search_alias_and_like_escape(tmp_db):
    store = CatalogStore(tmp_db)
    store.import_records(_records(), source="fixture", source_version="v1",
                         sha256="b" * 64)
    tmp_db.execute(
        "INSERT INTO instrument_aliases(instrument_id, alias, alias_type)"
        " VALUES(?,?,?)", ("CN:600519", "贵州茅台", "name"))
    hit = store.search(CatalogQuery(query="贵州茅台"))
    assert hit["total"] == 1
    # 用户输入的 % 不得变成通配符
    assert store.search(CatalogQuery(query="%"))["total"] == 0


def test_get_and_aliases(tmp_db):
    store = CatalogStore(tmp_db)
    store.import_records(_records(), source="fixture", source_version="v1",
                         sha256="c" * 64)
    item = store.get("CN:000001")
    assert item["name"] == "Ping An Bank"
    assert store.get("missing") is None
    assert store.by_symbol("SPY")[0]["asset_class"] == "etf"
    assert store.aliases("CN:000001") == []


def test_import_is_idempotent_by_sha(tmp_db):
    store = CatalogStore(tmp_db)
    first = store.import_records(_records(), source="fixture", source_version="v1",
                                 sha256="d" * 64)
    second = store.import_records(_records(), source="fixture", source_version="v1",
                                  sha256="d" * 64)
    assert second["idempotent"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert store.count() == 4
    assert len(store.snapshots()) == 1


def test_reimport_replaces_source_rows(tmp_db):
    store = CatalogStore(tmp_db)
    store.import_records(_records(), source="fixture", source_version="v1",
                         sha256="e" * 64)
    store.import_records([_records()[0]], source="fixture", source_version="v2",
                         sha256="f" * 64)
    assert store.count() == 1
    assert store.get("CN:600519")["source_version"] == "v2"
    assert store.get("CN:000001") is None


def test_instrument_id_venue_mapping():
    assert venue_code("600519.SS") == "CN"
    assert venue_code("000001.SZ") == "CN"
    assert venue_code("9988.HK") == "HK"
    assert venue_code("AAPL", "NMS", "United States") == "US"
    assert venue_code("^GSPC", "SNP") == "US"
    assert instrument_id_for("600519.SS") == "CN:600519.SS"
    assert instrument_id_for("AAPL", "NMS", "United States") == "US:AAPL"


def test_import_financedatabase_csv(tmp_path, tmp_db):
    root = tmp_path / "FinanceDatabase"
    database = root / "database"
    database.mkdir(parents=True)
    (database / "equities.csv").write_text(
        "symbol,name,currency,sector,industry,exchange,country,delisted\n"
        "600519.SS,Kweichow Moutai,CNY,Consumer Staples,Beverages,SHH,China,False\n"
        "OLD,Nano Corp,USD,Technology,Software,NMS,United States,True\n",
        encoding="utf-8")
    (database / "etfs.csv").write_text(
        "symbol,name,currency,category_group,category,exchange\n"
        "SPY,SPDR S&P 500 ETF Trust,USD,Equity,Large Blend,PCX\n",
        encoding="utf-8")

    store = CatalogStore(tmp_db)
    catalog_dir = tmp_path / "catalog"
    result = import_financedatabase(root, store, catalog_dir, as_of="2026-01-05")
    assert result["rows"] == 3
    assert result["license"] == "MIT"
    assert store.count() == 3
    assert store.get("CN:600519.SS")["valid_from"] == "2026-01-05"
    assert store.get("US:OLD")["listing_status"] == "delisted"
    assert store.get("US:SPY")["asset_class"] == "etf"

    manifest_path = Path(result["path"])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["license"] == "MIT"
    assert manifest["rows"] == 3
    assert manifest["asset_counts"] == {"equity": 2, "etf": 1}

    again = import_financedatabase(root, store, catalog_dir)
    assert again["idempotent"] is True
    assert store.count() == 3


def test_import_financedatabase_missing_layout(tmp_path, tmp_db):
    from backend.catalog.models import CatalogError

    root = tmp_path / "empty"
    root.mkdir()
    try:
        import_financedatabase(root, CatalogStore(tmp_db), tmp_path / "catalog")
    except CatalogError as e:
        assert "FinanceDatabase" in str(e)
        assert e.hint
    else:  # pragma: no cover
        raise AssertionError("应抛出 CatalogError")
