"""DEMO_MODE 冻结卡带预抓（答辩前一天执行）。

真实抓取落盘 → tests/cassettes_frozen/（随仓库分发，CI 复现冻结回放语义）：
  snapshot.json            全市场快照（东财）
  klines/<symbol>.json     演示白名单 K 线（后复权）
  financial/<symbol>.json  主要财务指标

现场断网也能完整演示（与提前录屏构成双保险）。
用法：python scripts/freeze_demo.py [--symbols 600519 000001 ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 与 tests/conftest.py 的 cassette_dir 同源（路径错位修复：统一为 tests/cassettes_frozen）
OUT = ROOT / "tests" / "cassettes_frozen"
DEFAULT_SYMBOLS = ["600519", "000001", "000858", "601318", "300750",
                   "000333", "002415", "601012", "600036", "601888", "510300"]


def freeze_snapshot(keep_symbols: list[str] | None = None) -> None:
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    if keep_symbols:
        # 默认只保留演示白名单行（仓库分发包 ≤ 数十 KB；答辩全量用 --full-snapshot）
        want = {s.zfill(6) for s in keep_symbols}
        df = df[df["代码"].astype(str).str.zfill(6).isin(want)].reset_index(drop=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "snapshot.json").write_text(
        json.dumps(df.to_dict(orient="records"), ensure_ascii=False, default=str),
        encoding="utf-8")
    print(f"✓ snapshot.json（{len(df)} 行）")


def freeze_kline(symbol: str) -> None:
    import akshare as ak

    start = (pd.Timestamp.now() - pd.Timedelta(days=500)).strftime("%Y%m%d")
    end = pd.Timestamp.now().strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                            start_date=start, end_date=end, adjust="hfq")
    rename = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
              "收盘": "close", "成交量": "volume", "成交额": "amount"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    path = OUT / "klines" / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(df.to_dict(orient="records"), default=str),
                    encoding="utf-8")
    print(f"✓ klines/{symbol}.json（{len(df)} 行）")


def freeze_financial(symbol: str) -> None:
    import akshare as ak
    import pandas as pd  # noqa: F401

    try:
        df = ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
        data = {"indicators": df.head(4).to_dict(orient="records")}
    except Exception as e:
        print(f"✗ 财务 {symbol}: {e}")
        return
    path = OUT / "financial" / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"✓ financial/{symbol}.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    parser.add_argument("--full-snapshot", action="store_true",
                        help="保留全市场快照（默认仅冻结 --symbols 白名单行，便于随仓库分发）")
    args = parser.parse_args()

    import pandas as pd  # noqa: F401  # freeze_kline 依赖

    freeze_snapshot(None if args.full_snapshot else args.symbols)
    for sym in args.symbols:
        try:
            freeze_kline(sym)
        except Exception as e:
            print(f"✗ kline {sym}: {e}")
        try:
            freeze_financial(sym)
        except Exception as e:
            print(f"✗ financial {sym}: {e}")
    print(f"\n卡带冻结完成 → {OUT}")
    print("答辩日启动: DEMO_MODE=1 python -m backend.app")


if __name__ == "__main__":
    main()
