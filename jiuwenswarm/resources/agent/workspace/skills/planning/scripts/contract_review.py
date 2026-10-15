"""Shared review protocol for method_design and experiment_plan.

Deterministic findings are produced locally; the LLM critic is reserved for the
semantic conflicts that require reading both artifacts.  Every finding has an
owner so a downstream editor receives only its own work list.
"""
from __future__ import annotations

from typing import Any

from scripts.validate_plan import ValidationError, validate_cross_refs

_OWNERS = {"method-design", "experiment-plan", "ledger"}


def deterministic_findings(method_design: dict, experiment_plan: dict, data_plan: dict,
                           inputs: dict) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for error in validate_cross_refs(
        method_design, experiment_plan, data_plan,
        key_papers=inputs.get("key_papers"), hypotheses=inputs.get("hypotheses"),
    ):
        assert isinstance(error, ValidationError)
        path = error.path
        owner = "method-design" if path.startswith(("innovation_points", "hypothesis_coverage", "components", "framework", "algorithm_reference")) else "experiment-plan"
        findings.append({
            "owner": owner,
            "path": path,
            "severity": "warning" if error.is_warning else "blocker",
            "reason": error.message,
            "source": "deterministic",
        })
    return findings


def normalize_contract_review(payload: Any) -> dict[str, Any]:
    """Make a model response safe to route without inventing a decision."""
    raw = payload if isinstance(payload, dict) else {}
    feedback: list[dict[str, str]] = []
    for item in raw.get("feedback") or []:
        if not isinstance(item, dict):
            continue
        owner = str(item.get("owner") or "").strip()
        path = str(item.get("path") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if owner in _OWNERS and path and reason:
            feedback.append({
                "owner": owner,
                "path": path,
                "severity": str(item.get("severity") or "major").strip(),
                "reason": reason,
                "proposed_patch": str(item.get("proposed_patch") or "").strip(),
            })
    decisions = [x for x in raw.get("decisions") or [] if isinstance(x, dict)]
    return {
        "passed": bool(raw.get("passed")) and not any(x["severity"] == "blocker" for x in feedback),
        "ledger_version": raw.get("ledger_version"),
        "decisions": decisions,
        "feedback": feedback,
        "summary": str(raw.get("summary") or "").strip(),
    }


def merge_findings(review: dict[str, Any], deterministic: list[dict[str, str]]) -> dict[str, Any]:
    merged = dict(review)
    merged["feedback"] = [*deterministic, *(review.get("feedback") or [])]
    merged["passed"] = bool(review.get("passed")) and not any(
        item.get("severity") == "blocker" for item in merged["feedback"]
    )
    return merged


def feedback_for_owner(review: dict[str, Any], owner: str) -> str:
    lines = []
    for item in review.get("feedback") or []:
        if item.get("owner") != owner:
            continue
        patch = item.get("proposed_patch") or "只修改此路径，保留未列字段。"
        lines.append(f"- [{item.get('severity', 'major')}] `{item.get('path')}`: {item.get('reason')}\n  修复: {patch}")
    if not lines:
        return ""
    return "[共享约定审查的定向修复清单]\n" + "\n".join(lines)
