from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .scanner import compute_content_hash, scan_skill
from .vocabulary import Finding


def _severity_order(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "extreme": 3}.get(value, -1)


def grade_from_findings(findings: list[Finding]) -> str:
    if not findings:
        return "unvetted"
    worst = max(findings, key=lambda f: f.severity._order)
    return worst.severity.value


@dataclass
class VetReport:
    grade: str
    content_hash: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    escalated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "content_hash": self.content_hash,
            "findings": self.findings,
            "created_at": self.created_at,
            "escalated": self.escalated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VetReport":
        return cls(
            grade=str(d.get("grade") or "unvetted"),
            content_hash=str(d.get("content_hash") or ""),
            findings=[f for f in d.get("findings", []) if isinstance(f, dict)],
            created_at=str(d.get("created_at") or ""),
            escalated=bool(d.get("escalated", False)),
        )


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    return {
        "category": f.category.value,
        "severity": f.severity.value,
        "file": f.file,
        "line": f.line,
        "evidence": f.evidence,
        "rule_id": f.rule_id,
    }


def build_report(
    skill_dir: Path,
    findings: list[Finding],
    content_hash: str,
    escalated: bool = False,
    grade: str | None = None,
) -> VetReport:
    return VetReport(
        grade=grade if grade is not None else grade_from_findings(findings),
        content_hash=content_hash,
        findings=[_finding_to_dict(f) for f in findings],
        escalated=escalated,
    )


def run_vet(skill_dir: Path, *, reviewer=None) -> VetReport:
    """Full vet pipeline: deterministic scan + optional advisory LLM escalation."""
    from .llm_escalation import review_findings

    content_hash = compute_content_hash(skill_dir)
    findings = scan_skill(skill_dir)

    # INTENTIONAL DEVIATION from the plan's verbatim Task 4 code (review fix).
    # The deterministic grade is a FLOOR for gating; LLM escalation is advisory.
    # The reviewer's findings still REPLACE the array, but a less-severe reviewer
    # result must never lower the reported grade below the scan's grade. We
    # therefore take the higher-severity of the two via _severity_order().
    # Do NOT "correct" this back to grading the post-escalation findings.
    deterministic_grade = grade_from_findings(findings)

    findings, escalated = review_findings(findings, skill_dir, reviewer=reviewer)

    escalated_grade = grade_from_findings(findings)
    if _severity_order(escalated_grade) > _severity_order(deterministic_grade):
        grade = escalated_grade
    else:
        grade = deterministic_grade

    return build_report(skill_dir, findings, content_hash, escalated=escalated, grade=grade)
