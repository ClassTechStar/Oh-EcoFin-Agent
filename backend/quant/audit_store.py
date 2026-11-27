"""回测与截断审计落库（M8/M9，表结构见方案报告 5.9）。

设计要点：
- `spec_hash` 由「策略 + 参数 + 数据区间」规范化 JSON 计算，同输入同哈希，
  可与截断检验、后续晋升决策交叉引用；
- 成本模型 / 跳过计数 / 数据时点 / 警告全部显式落库，拒绝「只存收益数字」；
- `leak_reports` 以回测记录为锚，passed=False 一票否决。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from backend.db import Database
from backend.quant.audit import LeakReport
from backend.quant.promotion import PromotionDecision

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def spec_hash(spec: dict) -> str:
    """规范化参数指纹（16 位十六进制，足够区分且不撑爆索引）。"""
    return hashlib.sha256(_canonical(spec).encode("utf-8")).hexdigest()[:16]


class AuditStore:
    """回测 / 泄漏报告的读写封装（同步；调用方负责线程池）。"""

    def __init__(self, db: Database):
        self.db = db

    # ---------- 回测 ----------

    def save_backtest(self, result: dict, *, strategy_id: str = "dual_ma",
                      spec: dict | None = None, run_id: str | None = None) -> str:
        """落库一次成功回测；返回 run_id。失败结果不入库（避免脏样本）。"""
        if not result.get("ok"):
            raise ValueError("回测未成功，拒绝落库")
        symbol = str(result.get("symbol") or "")
        spec = spec or {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "params": result.get("params") or {},
            "data_start": result.get("data_start"),
            "data_asof": result.get("data_asof"),
        }
        run_id = run_id or f"{strategy_id}-{symbol}-{int(time.time() * 1000)}"
        self.db.execute(
            "INSERT OR REPLACE INTO backtest_runs"
            "(id, strategy_id, spec_hash, start_date, end_date, metrics_json,"
            " cost_model_json, skipped_json, data_asof, provider, warnings_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, strategy_id, spec_hash(spec),
             str(result.get("data_start") or ""), str(result.get("data_asof") or ""),
             _dumps(result.get("metrics") or {}),
             _dumps(result.get("cost_model") or {}),
             _dumps(result.get("skipped") or {}),
             str(result.get("data_asof") or ""), str(result.get("provider") or ""),
             _dumps(result.get("warnings") or [])),
        )
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM backtest_runs WHERE id=?", (run_id,))
        if not row:
            return None
        return self._parse_run(row)

    def list_runs(self, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM backtest_runs ORDER BY rowid DESC LIMIT ?", (int(limit),))
        return [self._parse_run(r) for r in rows]

    def _parse_run(self, row: dict) -> dict:
        out = dict(row)
        for key, target in (("metrics_json", "metrics"),
                            ("cost_model_json", "cost_model"),
                            ("skipped_json", "skipped"),
                            ("warnings_json", "warnings")):
            raw = out.pop(key, None)
            try:
                out[target] = json.loads(raw) if raw else ({} if target != "warnings" else [])
            except json.JSONDecodeError:
                logger.warning("回测记录 %s 的 %s 解析失败", out.get("id"), key)
                out[target] = {}
        return out

    # ---------- 截断审计 ----------

    def save_leak(self, backtest_id: str, report: LeakReport) -> int:
        """落库一次截断检验；返回 leak_reports.id。"""
        return self.db.execute(
            "INSERT INTO leak_reports"
            "(backtest_id, cut_date, passed, trades_before_cut, leaks_json, skipped_reason)"
            " VALUES(?,?,?,?,?,?)",
            (backtest_id, report.cut_date, int(report.passed),
             int(report.fills_before_cut), _dumps(report.leaks),
             report.skipped_reason),
        )

    def list_leaks(self, backtest_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM leak_reports WHERE backtest_id=? ORDER BY id ASC",
            (backtest_id,))
        out = []
        for r in rows:
            item = dict(r)
            item["passed"] = bool(item["passed"])
            try:
                item["leaks"] = json.loads(item.pop("leaks_json") or "[]")
            except json.JSONDecodeError:
                item["leaks"] = []
                logger.warning("泄漏报告 %s 的 leaks_json 解析失败", item.get("id"))
            out.append(item)
        return out

    # ---------- 晋升决策（P3） ----------

    def save_promotion(self, decision: PromotionDecision, *, spec_hash: str = "",
                       backtest_id: str = "") -> int:
        return self.db.execute(
            "INSERT INTO promotions"
            "(strategy_id, spec_hash, backtest_id, approved, gates_json,"
            " reasons_json, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (decision.strategy_id, spec_hash, backtest_id, int(decision.approved),
             _dumps([g.to_dict() for g in decision.gates]),
             _dumps(decision.reasons),
             _now()),
        )

    def latest_promotion(self, strategy_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM promotions WHERE strategy_id=? ORDER BY id DESC LIMIT 1",
            (strategy_id,))
        if not row:
            return None
        item = dict(row)
        item["approved"] = bool(item["approved"])
        for key, target in (("gates_json", "gates"), ("reasons_json", "reasons")):
            raw = item.pop(key, None)
            try:
                item[target] = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                logger.warning("晋升记录 %s 的 %s 解析失败", item.get("id"), key)
                item[target] = []
        return item
