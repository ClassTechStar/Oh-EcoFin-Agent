"""M1-M3 API：目录检索、数据集上传、画像、只读 SQL 与审计。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings

CSV_TEXT = "date,region,amount\n2026-01-01,North,100.5\n2026-01-02,South,200\n2026-01-03,North,50\n"


def make_app(tmp_path) -> tuple[TestClient, object, Settings]:
    settings = Settings(db_path=str(tmp_path / "api.db"))
    settings.app.demo_mode = True
    settings.catalog.dir = str(tmp_path / "catalog")
    settings.datasets.dir = str(tmp_path / "datasets")
    settings.datasets.workspace_dir = str(tmp_path / "workspaces")
    app = create_app(settings)
    return TestClient(app), app.state.ctx, settings


@pytest.fixture()
def api(tmp_path):
    client, ctx, settings = make_app(tmp_path)
    with client:
        yield client, ctx, settings


def test_health_reports_data_dirs(api):
    client, _, settings = api
    data = client.get("/api/health").json()
    # 开箱首价值：空目录自动导入内置迷你标的（≥5 只），不再是 0
    assert data["catalog"]["instruments"] >= 5
    assert data["datasets"]["count"] == 0
    assert data["data_dirs"]["catalog"] == settings.catalog.dir
    assert data["data_dirs"]["datasets"] == settings.datasets.dir


def test_catalog_endpoints(api):
    client, ctx, _ = api
    baseline = ctx.catalog.count()  # 内置迷你目录的标的数
    ctx.catalog.import_records([{
        "instrument_id": "CN:600519", "symbol": "600519.SS", "exchange": "SHH",
        "asset_class": "equity", "name": "Kweichow Moutai", "country": "China",
        "sector": "Consumer Staples", "industry": "Beverages",
    }], source="fixture", source_version="v1", sha256="a" * 64)

    hit = client.get("/api/catalog/search", params={"q": "Moutai"}).json()
    assert hit["total"] == 1
    assert hit["items"][0]["instrument_id"] == "CN:600519"

    stats = client.get("/api/catalog/stats").json()
    assert stats["total"] == baseline + 1
    snap_sources = [s["source"] for s in stats["snapshots"]]
    assert "fixture" in snap_sources

    item = client.get("/api/catalog/instruments/CN:600519").json()
    assert item["instrument"]["name"] == "Kweichow Moutai"
    assert item["aliases"] == []
    assert client.get("/api/catalog/instruments/CN:missing").status_code == 404


def test_dataset_upload_profile_and_query(api):
    client, _, _ = api
    upload = client.post("/api/datasets", files={
        "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
    assert upload.status_code == 200
    payload = upload.json()
    dataset_id = payload["dataset_id"]
    table_name = payload["table_name"]
    assert payload["rows"] == 3

    listing = client.get("/api/datasets").json()
    assert listing["count"] == 1
    assert listing["datasets"][0]["dataset_id"] == dataset_id

    detail = client.get(f"/api/datasets/{dataset_id}").json()
    assert detail["dataset"]["schema"]["amount"] == "DOUBLE"

    versions = client.get(f"/api/datasets/{dataset_id}/versions").json()
    assert versions["versions"][0]["version"] == 1

    profile = client.get(f"/api/datasets/{dataset_id}/profile").json()
    assert profile["rows"] == 3
    assert {c["name"] for c in profile["profile"]} == {"date", "region", "amount"}

    tables = client.get("/api/query/tables").json()
    assert tables["tables"][0]["table_name"] == table_name

    query = client.post("/api/query", json={
        "sql": f"SELECT region, SUM(amount) AS total FROM {table_name}"
               " GROUP BY region ORDER BY total DESC"})
    assert query.status_code == 200
    result = query.json()
    assert result["rows"][0] == ["South", 200.0]
    assert result["result_hash"]

    blocked = client.post("/api/query", json={"sql": "DROP TABLE sales"})
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "not_read_only"

    bad_table = client.post("/api/query", json={"sql": "SELECT * FROM nope"})
    assert bad_table.status_code == 400
    assert "可用表" in bad_table.json()["detail"]["hint"]

    audit = client.get("/api/query/audit").json()["audit"]
    assert [a["ok"] for a in audit[:3]] == [False, False, True]


def test_upload_is_idempotent(api):
    client, _, _ = api
    first = client.post("/api/datasets", files={
        "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")}).json()
    second = client.post("/api/datasets", files={
        "file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")}).json()
    assert second["idempotent"] is True
    assert second["dataset_id"] == first["dataset_id"]
    assert len(client.get(f"/api/datasets/{first['dataset_id']}/versions").json()["versions"]) == 1


def test_upload_rejects_unsupported_type(api):
    client, _, _ = api
    response = client.post("/api/datasets", files={
        "file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400
    assert "不支持" in response.json()["detail"]


def test_catalog_import_endpoint(api, tmp_path):
    client, _, _ = api
    database = tmp_path / "fd" / "database"
    database.mkdir(parents=True)
    (database / "equities.csv").write_text(
        "symbol,name,currency,sector,industry,exchange,country,delisted\n"
        "AAPL,Apple Inc.,USD,Information Technology,Hardware,NMS,United States,False\n",
        encoding="utf-8")

    response = client.post("/api/catalog/import", json={"path": str(tmp_path / "fd")})
    assert response.status_code == 200
    assert response.json()["rows"] == 1
    assert client.get("/api/catalog/search", params={"q": "Apple"}).json()["total"] == 1

    again = client.post("/api/catalog/import", json={"path": str(tmp_path / "fd")})
    assert again.json()["idempotent"] is True

    missing = client.post("/api/catalog/import", json={"path": str(tmp_path / "nope")})
    assert missing.status_code == 400
