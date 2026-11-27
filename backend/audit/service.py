"""可信审计服务（M7）：证据账本 + 数字比对 + 质量闸门。

门禁：
  G1_SOURCE   每块有结果哈希/数据集 sha
  G2_EVIDENCE 数值单元格登记为证据（超上限即失败）
  G3_NUMBERS  文本数字全部能匹配到证据（缺证据阻断）
  G4_CHART    图表有 spec_hash/result_hash
  G5_MANIFEST 报告有 manifest 与数据集 sha
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from backend.audit.evidence import build_evidence
from backend.audit.numbers import claim_candidates, extract_claims
from backend.audit.store import AuditStore
from backend.config import Settings


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _claim_id(report_id: str, block_id: str, index: int, claim: str,
              value: float) -> str:
    raw = f"{report_id}|{block_id}|{index}|{claim}|{value}"
    return "nc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class AuditService:
    def __init__(self, db, settings: Settings):
        self.store = AuditStore(db)
        self.config = settings.audit

    def audit_report(self, spec: dict) -> dict:
        report_id = spec["report_id"]
        evidence, evidence_truncated = build_evidence(
            spec, int(self.config.max_evidence))
        numeric_index = [(item["value_num"], item["id"])
                         for item in evidence
                         if item.get("value_num") is not None]
        claims: list[dict] = []
        claims_truncated = False
        for item in evidence:
            if item.get("value_num") is None:
                continue
            if len(claims) >= int(self.config.max_claims):
                claims_truncated = True
                break
            claims.append({
                "id": _claim_id(report_id, item["block_id"], len(claims),
                                "结构化数值", float(item["value_num"])),
                "report_id": report_id,
                "block_id": item["block_id"],
                "claim": "结构化数值",
                "value": float(item["value_num"]),
                "unit": "",
                "evidence_ids": [item["id"]],
                "passed": True,
            })

        for block in spec.get("blocks") or []:
            if block.get("type") != "text":
                continue
            for index, claim in enumerate(extract_claims(block.get("text") or "")):
                if len(claims) >= int(self.config.max_claims):
                    claims_truncated = True
                    break
                matches: list[str] = []
                for candidate in claim_candidates(claim["value"], claim["unit"]):
                    for evidence_value, evidence_id in numeric_index:
                        tolerance = max(abs(float(evidence_value))
                                        * float(self.config.tolerance),
                                        float(self.config.tolerance))
                        if abs(float(evidence_value) - candidate) <= tolerance:
                            if evidence_id not in matches:
                                matches.append(evidence_id)
                claims.append({
                    "id": _claim_id(report_id, block["id"], index,
                                    claim["claim"], float(claim["value"])),
                    "report_id": report_id,
                    "block_id": block["id"],
                    "claim": claim["claim"],
                    "value": float(claim["value"]),
                    "unit": claim["unit"],
                    "evidence_ids": matches[:10],
                    "passed": bool(matches),
                    "context": claim.get("context", ""),
                })
            if claims_truncated:
                break

        claims_failed = sum(1 for claim in claims if not claim["passed"])
        violations: list[dict] = []
        gates: list[dict] = []

        source_problems = self._check_sources(spec)
        gates.append(self._gate("G1_SOURCE", not source_problems,
                                "; ".join(source_problems)))
        violations.extend({
            "level": "error", "gate": "G1_SOURCE", "detail": problem,
        } for problem in source_problems)

        evidence_problems = []
        if evidence_truncated:
            evidence_problems.append(
                f"证据数量超过上限 {self.config.max_evidence}，已截断")
        if claims_truncated:
            evidence_problems.append(
                f"数字声明超过上限 {self.config.max_claims}，已截断")
        gates.append(self._gate("G2_EVIDENCE", not evidence_problems,
                                "; ".join(evidence_problems)))
        violations.extend({
            "level": "error", "gate": "G2_EVIDENCE", "detail": problem,
        } for problem in evidence_problems)

        number_problems = [
            f"块 {claim['block_id']} 数字 {claim['claim']} 未找到证据"
            for claim in claims if not claim["passed"]
        ]
        if claims_failed:
            number_problems.append(f"共 {claims_failed} 个数字缺证据")
        gates.append(self._gate("G3_NUMBERS", not number_problems,
                                "; ".join(number_problems)))
        violations.extend({
            "level": "error", "gate": "G3_NUMBERS", "detail": problem,
        } for problem in number_problems)

        chart_problems = []
        for block in spec.get("blocks") or []:
            chart = block.get("chart") or {}
            if not chart:
                continue
            if not chart.get("spec_hash"):
                chart_problems.append(f"块 {block.get('id')} 图表缺少 spec_hash")
            if not chart.get("result_hash"):
                chart_problems.append(f"块 {block.get('id')} 图表缺少 result_hash")
        gates.append(self._gate("G4_CHART", not chart_problems,
                                "; ".join(chart_problems)))
        violations.extend({
            "level": "error", "gate": "G4_CHART", "detail": problem,
        } for problem in chart_problems)

        manifest_problems = []
        if not spec.get("manifest"):
            manifest_problems.append("报告缺少 manifest")
        for dataset in spec.get("datasets") or []:
            if not dataset.get("sha256"):
                manifest_problems.append(
                    f"数据集 {dataset.get('dataset_id')} 缺少 sha256")
        gates.append(self._gate("G5_MANIFEST", not manifest_problems,
                                "; ".join(manifest_problems)))
        violations.extend({
            "level": "error", "gate": "G5_MANIFEST", "detail": problem,
        } for problem in manifest_problems)

        status = "passed" if not violations else "blocked"
        audit = {
            "status": status,
            "report_id": report_id,
            "spec_hash": spec.get("spec_hash", ""),
            "audited_at": _now(),
            "evidence_count": len(evidence),
            "claims_total": len(claims),
            "claims_passed": len(claims) - claims_failed,
            "claims_failed": claims_failed,
            "gates": gates,
            "violations": violations,
            "evidence_truncated": evidence_truncated,
            "claims_truncated": claims_truncated,
        }
        self.store.replace(report_id, evidence, claims, violations[:500])
        return audit

    def get(self, report_id: str) -> dict:
        data = self.store.get(report_id)
        return {**data, **self.store.summary(report_id)}

    @staticmethod
    def _gate(name: str, passed: bool, detail: str = "") -> dict:
        return {"gate": name, "status": "pass" if passed else "fail",
                "detail": detail if not passed else ""}

    @staticmethod
    def _check_sources(spec: dict) -> list[str]:
        problems: list[str] = []
        for block in spec.get("blocks") or []:
            source = block.get("source") or {}
            block_id = block.get("id")
            if block.get("type") == "analysis":
                if not source.get("result_hash"):
                    problems.append(f"块 {block_id} 缺少 result_hash")
                if not source.get("dataset_sha256"):
                    problems.append(f"块 {block_id} 缺少数据集 sha256")
            elif block.get("type") == "sql":
                if not source.get("sql_hash"):
                    problems.append(f"块 {block_id} 缺少 sql_hash")
                if not source.get("result_hash"):
                    problems.append(f"块 {block_id} 缺少 result_hash")
        return problems
