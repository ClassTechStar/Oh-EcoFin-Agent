"""标的目录契约（M1）。

设计依据：方案 6.6 Instrument 契约 + FinanceDatabase `equities_controller.py:12-30`
的字段清单。目录只回答“存在哪些标的、属于哪个行业/交易所”，不携带行情与基本面。

与源项目的差异：FinanceDatabase 是周更覆盖式快照（无 valid_from/valid_to），
这里为每批导入写入 valid_from/source_version，使目录版本可追溯。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AssetClass = Literal["equity", "etf", "fund", "index", "fx", "crypto", "moneymarket"]
ListingStatus = Literal["active", "delisted", "unknown"]


class Instrument(BaseModel):
    instrument_id: str
    symbol: str
    exchange: str = ""
    asset_class: AssetClass = "equity"
    name: str = ""
    currency: str = ""
    sector: str = ""
    industry: str = ""
    country: str = ""
    listing_status: ListingStatus = "active"
    is_primary: bool = True
    valid_from: str = ""
    valid_to: str | None = None
    source: str = ""
    source_version: str = ""


class CatalogQuery(BaseModel):
    """检索条件：所有文本条件均为包含匹配（大小写不敏感）。"""

    query: str = Field("", max_length=100)
    symbol: str = Field("", max_length=32)
    exchange: str = Field("", max_length=16)
    asset_class: str = Field("", max_length=20)
    industry: str = Field("", max_length=80)
    sector: str = Field("", max_length=80)
    country: str = Field("", max_length=80)
    listing_status: str = Field("active", max_length=16)
    is_primary: bool | None = None
    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)


class CatalogSnapshot(BaseModel):
    snapshot_id: str
    source: str
    source_version: str
    sha256: str
    rows: int
    path: str = ""
    imported_at: str
    idempotent: bool = False


class CatalogError(ValueError):
    """目录导入/检索的业务错误（可带可执行提示）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint
