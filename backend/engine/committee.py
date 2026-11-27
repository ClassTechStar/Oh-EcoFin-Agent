"""委员会协议（M6）：不可变上下文 + 隔离角色 + 六段原文 + 禁止收敛伪造。

移植自 openInvest `committee-protocol.md` 的可执行契约（见方案报告 1.6）：
- Stage 0 证据新鲜度：`analysis_as_of` 与事实集指纹一并进入快照；
- Stage 1 不可变上下文：`context_id = sha256(规范化 payload)`，所有角色只读同一快照；
- Stage 2 独立首轮：每角色一次调用、互不共享消息（隔离），失败必须可见；
- Stage 3 交叉质询：第二轮拿到第一轮全文，改判必须给出 `change_reason`；
- Stage 4 可选多轮：轮数由配置上限约束，保留真实分歧；
- Stage 5/6 CIO 汇总与提交：六段原文 + 精确 `context_id` 落库，可回放。

六段审计视图（缺失返回 None —— 让「没跑」可见，而不是伪造成中性意见）：
    analyst（证据准备）→ bull_r1 / bear_r1（独立首轮）
    → bull_r2 / bear_r2（交叉质询）→ cio（汇总）
风险复核是质询后的独立闸门，单列 `risk` 段，不计入六段。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: 固定角色→输入映射：角色拿到什么由协议决定，不由调用方临时拼（Stage 1）
ROLE_INPUTS: dict[str, list[str]] = {
    "analyst": ["market", "financial", "news", "valuation", "indicators"],
    "bull": ["datapack"],
    "bear": ["datapack"],
    "risk": ["datapack", "bull", "bear"],
    "cio": ["datapack", "bull", "bear", "risk"],
}

#: 六段审计视图（与协议阶段一一对应，缺失即 None）
SEGMENTS: tuple[str, ...] = (
    "analyst", "bull_r1", "bear_r1", "bull_r2", "bear_r2", "cio",
)

#: 角色失败时的占位文案：显式失败，不伪造中性意见
FAILED_PREFIX = "角色执行失败"


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _canonical(payload: object) -> str:
    """规范化 JSON：键排序 + 中文不转义 → 同输入同哈希。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class ContextSnapshot:
    """不可变上下文：一次辩论的全部输入（事实集 + 时点 + 用户问题）。"""

    context_id: str
    analysis_as_of: str
    symbols: tuple[str, ...]
    user_question: str
    fact_count: int
    payload: str

    def role_inputs(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in ROLE_INPUTS.items()}

    def to_audit_dict(self) -> dict:
        return {
            "context_id": self.context_id,
            "analysis_as_of": self.analysis_as_of,
            "symbols": list(self.symbols),
            "user_question": self.user_question,
            "fact_count": self.fact_count,
            "payload_sha256": hashlib.sha256(self.payload.encode("utf-8")).hexdigest(),
            "role_inputs": self.role_inputs(),
        }


def build_context(symbols: list[str], facts: list[dict], analysis_as_of: str,
                  user_question: str = "") -> ContextSnapshot:
    """构造不可变快照；同一事实集 + 同一时点 → 同一 `context_id`。"""
    payload = _canonical({
        "symbols": list(symbols),
        "analysis_as_of": analysis_as_of,
        "user_question": user_question,
        "facts": facts,
    })
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ContextSnapshot(
        context_id=f"ctx_{digest[:16]}",
        analysis_as_of=analysis_as_of,
        symbols=tuple(symbols),
        user_question=user_question,
        fact_count=len(facts),
        payload=payload,
    )


@dataclass
class RoleTurn:
    """单个角色单轮原文（审计最小单元）。"""

    role: str
    round: int
    text: str = ""
    status: str = "ok"           # ok | failed
    error: str = ""
    model: str = ""
    context_id: str = ""
    signal: float | None = None
    strength: float | None = None
    change_reason: str = ""
    latency_ms: int = 0
    merged: bool = False          # True = 多空合并一次调用（1 轮降级）

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "round": self.round,
            "status": self.status,
            "text": self.text,
            "error": self.error,
            "model": self.model,
            "context_id": self.context_id,
            "signal": self.signal,
            "strength": self.strength,
            "change_reason": self.change_reason,
            "latency_ms": self.latency_ms,
            "merged": self.merged,
        }


@dataclass
class CommitteeTranscript:
    """一次辩论的完整原文记录 + 协议校验。"""

    context: ContextSnapshot
    turns: list[RoleTurn] = field(default_factory=list)
    verification_required: bool = False
    verification_notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    # ---------- 记录 ----------

    def add(self, turn: RoleTurn) -> RoleTurn:
        if not turn.context_id:
            turn.context_id = self.context.context_id
        self.turns.append(turn)
        return turn

    def ok_turn(self, role: str, round: int, text: str, **kwargs) -> RoleTurn:
        return self.add(RoleTurn(role=role, round=round, text=text,
                                 status="ok", **kwargs))

    def failed_turn(self, role: str, round: int, error: str, **kwargs) -> RoleTurn:
        """失败落痕：原文必须为空、错误必须非空，绝不填充中性意见。"""
        return self.add(RoleTurn(role=role, round=round, text="", status="failed",
                                 error=error or FAILED_PREFIX, **kwargs))

    def failed_turns(self) -> list[RoleTurn]:
        return [t for t in self.turns if t.status == "failed"]

    def text_of(self, role: str, round: int) -> str | None:
        turn = next((t for t in self.turns
                     if t.role == role and t.round == round and t.status == "ok"), None)
        return turn.text if turn else None

    # ---------- 校验 ----------

    def segments(self) -> dict[str, str | None]:
        """六段审计视图；缺失段为 None（可见的空缺）。"""
        out: dict[str, str | None] = {}
        for seg in SEGMENTS:
            if seg in ("analyst", "cio"):
                out[seg] = self.text_of(seg, 0)
                continue
            role, _, rnd = seg.rpartition("_r")
            out[seg] = self.text_of(role, int(rnd))
        return out

    def validate(self) -> list[str]:
        """协议断言：context_id 一致 / 失败可见 / 改判有解释 / 成功有原文。"""
        issues: list[str] = []
        ctx_id = self.context.context_id
        for t in self.turns:
            if t.context_id != ctx_id:
                issues.append(f"{t.role} r{t.round} 的 context_id={t.context_id} "
                              f"与快照 {ctx_id} 不一致")
            if t.status == "failed":
                if t.text.strip():
                    issues.append(f"{t.role} r{t.round} 失败却带原文（疑似伪造意见）")
                if not t.error.strip():
                    issues.append(f"{t.role} r{t.round} 失败但未记录错误")
                if t.signal is not None:
                    issues.append(f"{t.role} r{t.round} 失败却给出 signal（疑似伪造）")
            elif not t.text.strip():
                issues.append(f"{t.role} r{t.round} 成功但原文为空")

        first_round = {t.role: t for t in self.turns
                       if t.round == 1 and t.status == "ok" and t.signal is not None}
        for t in self.turns:
            if t.round != 2 or t.status != "ok" or t.signal is None:
                continue
            prev = first_round.get(t.role)
            if (prev is not None
                    and abs(t.signal - prev.signal) > 1e-9
                    and not t.change_reason.strip()):
                issues.append(f"{t.role} r2 改判 signal {prev.signal}→{t.signal} 但未说明理由")

        if self.failed_turns() and not self.verification_required:
            issues.append("存在失败角色但 verification_required=False（失败被静默）")
        return issues

    def to_audit_dict(self) -> dict:
        return {
            "context": self.context.to_audit_dict(),
            "segments": self.segments(),
            "risk_review": self.text_of("risk", 1),
            "turns": [t.to_dict() for t in self.turns],
            "verification_required": self.verification_required,
            "verification_notes": list(self.verification_notes),
            "issues": self.validate(),
            "created_at": self.created_at,
        }


# ---------- 落库与回放（报告 5.9 debate_runs / debate_sections） ----------

def persist_audit(db, audit: dict, *, symbol: str, verdict: str,
                  status: str = "ok", run_id: str | None = None) -> str:
    """把一次辩论的六段原文写入 SQLite；返回 run_id。"""
    ctx = audit.get("context") or {}
    context_id = str(ctx.get("context_id") or "")
    run_id = run_id or f"{context_id or 'ctx'}-{int(time.time() * 1000)}"
    created = str(audit.get("created_at") or _now())
    db.execute(
        "INSERT OR REPLACE INTO debate_runs"
        "(id, symbol, as_of, context_id, status, verdict, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (run_id, symbol, str(ctx.get("analysis_as_of") or ""), context_id,
         status, verdict, created),
    )
    for turn in audit.get("turns", []):
        db.execute(
            "INSERT OR REPLACE INTO debate_sections"
            "(run_id, role, round, raw_text, model, signal, strength, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (run_id, str(turn.get("role") or ""), int(turn.get("round") or 0),
             str(turn.get("text") or ""), str(turn.get("model") or ""),
             turn.get("signal"), turn.get("strength"), created),
        )
    return run_id


def load_audit(db, run_id: str) -> dict | None:
    """回放一次辩论：run 元数据 + 按角色/轮次排序的原文。"""
    run = db.query_one("SELECT * FROM debate_runs WHERE id=?", (run_id,))
    if not run:
        return None
    sections = db.query(
        "SELECT role, round, raw_text, model, signal, strength FROM debate_sections"
        " WHERE run_id=? ORDER BY round ASC, role ASC", (run_id,))
    return {"run": run, "sections": sections}
