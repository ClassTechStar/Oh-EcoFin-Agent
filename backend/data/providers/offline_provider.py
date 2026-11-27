"""离线 Provider：读取冻结卡带（DEMO_MODE）或确定性合成数据（测试）。

能力与真实 Provider 完全同契约——这是「离线可完整验证」原则的落点：
CI 与答辩现场断网时，上层工具/引擎/前端一行不改即可运行。
"""

from __future__ import annotations

import json
import logging
import zlib
from pathlib import Path

import pandas as pd

from backend.data.providers.base import (MarketDataProvider, ProviderUnavailable,
                                         SPOT_COLUMNS, normalize_daily)
from backend.quant.fixtures import synthetic_ohlcv

logger = logging.getLogger(__name__)


class OfflineProvider(MarketDataProvider):
    """冻结卡带优先；`synthetic_fallback=True` 时缺失文件用确定性合成数据兜底（仅测试）。"""

    name = "offline"
    CAPABILITIES = frozenset({"spot_all", "daily_bar", "financial"})

    def __init__(self, cassette_dir: str | Path, synthetic_fallback: bool = False):
        self.cassette_dir = Path(cassette_dir)
        self.synthetic_fallback = synthetic_fallback
        self.last_source = "cassette"

    # ---------- 快照 ----------

    def spot_all(self) -> pd.DataFrame:
        path = self.cassette_dir / "snapshot.json"
        if not path.exists():
            raise ProviderUnavailable(
                f"DEMO_MODE 卡带缺失: {path}",
                hint="先运行 python scripts/freeze_demo.py 预抓数据",
            )
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ProviderUnavailable(f"卡带损坏: {path}", hint="重新执行 freeze_demo.py") from e
        if not rows:
            raise ProviderUnavailable("卡带为空", hint="重新执行 freeze_demo.py")

        df = pd.DataFrame(rows)
        # 兼容两种卡带形态：规范列 或 旧版中文列（历史冻结数据）
        legacy = {"代码": "symbol", "名称": "name", "最新价": "price", "涨跌幅": "pct_chg",
                  "成交量": "volume", "成交额": "amount", "换手率": "turnover",
                  "量比": "volume_ratio", "总市值": "market_cap",
                  "市盈率-动态": "pe", "市净率": "pb"}
        df = df.rename(columns={k: v for k, v in legacy.items() if k in df.columns})
        if "symbol" not in df.columns:
            raise ProviderUnavailable("卡带缺 symbol 列", hint="重新执行 freeze_demo.py")
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        for col in SPOT_COLUMNS:
            if col not in df.columns:
                df[col] = float("nan") if col not in ("name", "ts") else ""
        self.last_source = "cassette"
        return df[SPOT_COLUMNS]

    # ---------- 日线 ----------

    def daily_bar(self, symbol: str, days: int = 120, adjust: str = "hfq") -> pd.DataFrame:
        symbol = str(symbol).zfill(6)
        path = self.cassette_dir / "klines" / f"{symbol}.json"
        if path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                df = normalize_daily(pd.DataFrame(rows), {})
                self.last_source = "cassette"
                return df.tail(days).reset_index(drop=True)
            except (json.JSONDecodeError, ProviderUnavailable) as e:
                logger.warning("K线卡带不可用(%s): %s", symbol, e)

        if not self.synthetic_fallback:
            raise ProviderUnavailable(
                f"DEMO_MODE K线卡带缺失: {path}",
                hint="先运行 python scripts/freeze_demo.py 预抓数据",
            )
        # 测试兜底：确定性合成（同标的同长度 → 逐字节一致）
        seed = zlib.crc32(symbol.encode()) % 1000
        df = synthetic_ohlcv(max(days, 120), seed=seed)
        self.last_source = "synthetic"
        return df.tail(days).reset_index(drop=True)

    # ---------- 财务 ----------

    def financial_indicators(self, symbol: str) -> list[dict]:
        path = self.cassette_dir / "financial" / f"{str(symbol).zfill(6)}.json"
        if not path.exists():
            if self.synthetic_fallback:
                return [{"报告期": "2026-09-30", "营业收入": 100.0,
                         "净利润": 20.0, "同比增长": 5.0}]
            raise ProviderUnavailable(
                f"DEMO_MODE 财务卡带缺失: {path}",
                hint="先运行 python scripts/freeze_demo.py 预抓数据",
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("indicators", [])
