"""决策记忆：每次研判落库（标的/结论/数据快照），演示"记得上次的错误"。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from backend.db import Database

logger = logging.getLogger(__name__)


def _now() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class DecisionStore:
    def __init__(self, db: Database, snapshot_dir: str = "data/cache/decisions"):
        self.db = db
        self.snapshot_dir = Path(snapshot_dir)

    def save_decision(self, symbol: str, verdict: str, thesis_digest: str,
                      data_pack: dict, data_as_of: str, horizon_days: int = 5) -> int:
        """研判落库 + 数据快照冻结（复盘的"当时依据"）。"""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = ""
        try:
            ts = _now().replace(":", "").replace(" ", "_").replace("-", "")
            p = self.snapshot_dir / f"{symbol}_{ts}.json"
            p.write_text(json.dumps(data_pack, ensure_ascii=False, default=str),
                         encoding="utf-8")
            snapshot_path = str(p)
        except OSError:
            logger.warning("决策快照冻结失败", exc_info=True)
        return self.db.execute(
            "INSERT INTO decisions(symbol, verdict, thesis_digest, snapshot_path,"
            " data_as_of, horizon_days, created_at) VALUES(?,?,?,?,?,?,?)",
            (symbol, verdict, thesis_digest, snapshot_path, data_as_of,
             horizon_days, _now()),
        )

    def history_for(self, symbol: str, limit: int = 5) -> list[dict]:
        """同标的历史决策（含结算与反思）→ 注入后续分析 prompt。"""
        return self.db.query(
            "SELECT id, symbol, verdict, thesis_digest, data_as_of, horizon_days,"
            " settled, settled_at, reflection, created_at FROM decisions"
            " WHERE symbol=? AND settled IS NOT NULL ORDER BY id DESC LIMIT ?",
            (symbol, limit))

    def pending_decisions(self) -> list[dict]:
        """未结算决策（T+N 定时结算扫描用）。"""
        return self.db.query(
            "SELECT id, symbol, verdict, thesis_digest, data_as_of, horizon_days,"
            " snapshot_path, created_at FROM decisions WHERE settled IS NULL")

    def mark_settled(self, decision_id: int, settled_return: float,
                     reflection: str | None) -> None:
        self.db.execute(
            "UPDATE decisions SET settled=?, settled_at=?, reflection=? WHERE id=?",
            (settled_return, _now(), reflection, decision_id))


def direction_ok(verdict: str, actual_return: float) -> bool:
    """结论方向 vs 实际走势 → 盈亏判定。watch 不判对错。"""
    if verdict == "bullish":
        return actual_return > 0
    if verdict == "bearish":
        return actual_return < 0
    return True
