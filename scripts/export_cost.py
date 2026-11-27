"""成本清单导出（交付物 5）：llm_calls 表 → Markdown + CSV 逐项明细。

用法：python scripts/export_cost.py [--db data/finagent.db] [--out docs/cost_report]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.db import Database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "finagent.db"))
    parser.add_argument("--out", default=str(ROOT / "docs" / "cost_report"))
    args = parser.parse_args()

    db = Database(args.db)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = db.query(
        "SELECT created_at, model, tier, role, tokens_in, tokens_out, cached_tokens,"
        " cost_cents, latency_ms FROM llm_calls ORDER BY id")
    total = sum(r["cost_cents"] for r in rows)
    by_model: dict[str, dict] = {}
    for r in rows:
        key = f"{r['model']}({r['tier']})"
        agg = by_model.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                        "cached": 0, "cost_cents": 0})
        agg["calls"] += 1
        agg["tokens_in"] += r["tokens_in"]
        agg["tokens_out"] += r["tokens_out"]
        agg["cached"] += r["cached_tokens"]
        agg["cost_cents"] += r["cost_cents"]

    # CSV 明细
    csv_path = out_dir / "llm_calls.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "模型", "tier", "角色", "输入tokens", "输出tokens",
                    "缓存tokens", "成本(分)", "延迟ms"])
        for r in rows:
            w.writerow([r["created_at"], r["model"], r["tier"], r["role"],
                        r["tokens_in"], r["tokens_out"], r["cached_tokens"],
                        r["cost_cents"], r["latency_ms"]])

    # Markdown 汇总
    md = ["# 全周期 API 成本清单", "",
          f"- 调用总次数：{len(rows)}",
          f"- 总成本：**¥{total / 100:.2f}**",
          "- 预算目标：<¥100（消融实验集中执行口径见方案第 17 章）",
          "", "| 模型 | 调用次数 | 输入tokens | 输出tokens | 缓存tokens | 成本(元) |",
          "|---|---|---|---|---|---|"]
    for key, agg in by_model.items():
        md.append(f"| {key} | {agg['calls']} | {agg['tokens_in']} | {agg['tokens_out']} "
                  f"| {agg['cached']} | ¥{agg['cost_cents'] / 100:.2f} |")
    md_path = out_dir / "cost_summary.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    print(f"✓ 明细 CSV: {csv_path}")
    print(f"✓ 汇总 MD: {md_path}")
    print(f"总成本: ¥{total / 100:.2f}（{len(rows)} 次调用）")


if __name__ == "__main__":
    main()
