"""L1 反馈回流：每周 feedback digest（失败模式汇总 → Markdown 报告）。

用法：python scripts/feedback_digest.py [--days 7] [--out docs/]
- 有 GLM/DeepSeek 密钥：economy 模型汇总 👎 反馈的失败模式（增量成本 ≈ 1 次调用）；
- 无密钥：确定性统计（rating 分布 / 版本分桶 / 原样罗列 👎 评论），照样可留档。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import load_settings  # noqa: E402
from backend.db import Database  # noqa: E402

DIGEST_PROMPT = """以下是近 {days} 天用户对智能体的反馈（👍/👎 + 文字）。
请汇总 👎 反映的失败模式（≤8 条，每条一句话），并给 1 条最优先的改进建议。
只输出 Markdown 列表，不要客套。

反馈样本：
{feedback}
"""


def fetch_feedback(db: Database, days: int) -> list[dict]:
    since = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y-%m-%d")
    return db.query(
        "SELECT session_id, run_id, rating, comment, prompt_version, created_at"
        " FROM feedback WHERE created_at >= ? ORDER BY created_at DESC", (since,))


def render_stats(rows: list[dict], days: int) -> str:
    total = len(rows)
    ups = sum(1 for r in rows if r["rating"] == "up")
    downs = total - ups
    by_version: dict[str, dict[str, int]] = {}
    for r in rows:
        bucket = by_version.setdefault(r["prompt_version"] or "unknown", {"up": 0, "down": 0})
        bucket[r["rating"]] = bucket.get(r["rating"], 0) + 1
    lines = [
        f"# Feedback Digest（近 {days} 天）",
        "",
        f"- 样本 {total} 条：👍 {ups} / 👎 {downs}"
        + (f"（好评率 {ups / total:.0%}）" if total else ""),
        "",
        "## 按 Prompt 版本分桶（L3 A/B 数据面）",
        "",
        "| 版本 | 👍 | 👎 |",
        "|---|---|---|",
    ]
    for version, bucket in sorted(by_version.items()):
        lines.append(f"| {version} | {bucket.get('up', 0)} | {bucket.get('down', 0)} |")
    negatives = [r for r in rows if r["rating"] == "down" and r["comment"]]
    lines += ["", f"## 👎 文字反馈（{len(negatives)} 条，原样留档）", ""]
    lines += [f"- [{r['created_at']}] {r['comment']}（session {r['session_id'][:8]}）"
              for r in negatives[:50]]
    return "\n".join(lines) + "\n"


async def llm_failure_modes(rows: list[dict], days: int) -> str:
    from backend.db import AsyncDatabase
    from backend.engine.gateway import CostLedger, LLMGateway
    from backend.security.keyring_loader import load_key

    glm_key = load_key("glm_key")
    deepseek_key = load_key("deepseek_key")
    if not (glm_key or deepseek_key):
        return ""
    settings = load_settings()
    ledger = CostLedger(AsyncDatabase(Database(settings.db_path)),
                        settings.llm.daily_cost_cap_cny)
    gw = LLMGateway(settings, ledger, glm_key, deepseek_key)
    sample = "\n".join(
        f"[{r['rating']}] {r['comment'] or '（无文字）'}"
        for r in rows[:80])
    text = await gw.simple(
        [{"role": "user", "content": DIGEST_PROMPT.format(days=days, feedback=sample)}],
        "economy", role="feedback_digest", max_tokens=600)
    return (text or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="docs/")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(settings.db_path)
    rows = fetch_feedback(db, args.days)
    md = render_stats(rows, args.days)

    import asyncio

    try:
        modes = asyncio.run(llm_failure_modes(rows, args.days))
    except Exception as e:
        print(f"LLM 汇总跳过：{type(e).__name__}: {e}")
        modes = ""
    if modes:
        md += "\n## 失败模式汇总（economy 模型）\n\n" + modes + "\n"

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"feedback_digest_{time.strftime('%Y%m%d')}.md"
    path.write_text(md, encoding="utf-8")
    print(f"反馈样本 {len(rows)} 条 → {path}")


if __name__ == "__main__":
    main()
