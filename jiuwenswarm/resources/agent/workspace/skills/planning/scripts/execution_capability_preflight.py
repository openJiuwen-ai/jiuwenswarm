"""Deterministic upstream-to-experiment capability handshake.

This is deliberately not an LLM review.  It reads the experiment module's
published capability contract and only blocks an explicitly recognized
requirement profile.  A planner must not spend model calls "repairing" a
research goal when the required executor and metric calculator do not exist.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_CAPABILITIES_PATH = (
    Path(__file__).resolve().parents[2]
    / "experiment"
    / "references"
    / "execution-capabilities.json"
)


def assess_execution_capability(inputs: dict[str, Any]) -> dict[str, Any]:
    """Return a stable capability report for module-one requirements.

    The input is searched only for the explicitly declared profile signals.
    This avoids pretending that a broad natural-language task classifier is a
    trustworthy execution decision.
    """
    contract = _load_contract()
    corpus = _requirement_text(inputs).casefold()
    blockers: list[dict[str, Any]] = []
    available_profiles: list[dict[str, Any]] = []
    for profile in contract.get("available_requirement_profiles", []):
        if isinstance(profile, dict) and _profile_matches(profile, corpus):
            available_profiles.append({
                key: profile.get(key)
                for key in (
                    "profile_id", "executor_id", "task_type", "method_ids",
                    "primary_method_id", "baseline_method_ids",
                    "metric_names", "preferred_datasets",
                    "unsupported_method_claims", "planning_constraints",
                )
            })
    for profile in contract.get("unavailable_requirement_profiles", []):
        if not isinstance(profile, dict) or not _profile_matches(profile, corpus):
            continue
        blockers.append({
            "profile_id": profile.get("profile_id", "unknown"),
            "kind": "registered_executor_missing",
            "missing_capabilities": list(profile.get("missing_capabilities") or []),
            "next_action": profile.get("next_action", "Implement a registered experiment executor."),
        })
    return {
        "passed": not blockers,
        "contract_path": str(_CAPABILITIES_PATH),
        "contract_schema_version": contract.get("schema_version"),
        "available_profiles": available_profiles,
        "blockers": blockers,
    }


def format_capability_errors(report: dict[str, Any]) -> list[str]:
    """Render operator-facing errors without losing structured detail on disk."""
    errors: list[str] = []
    for blocker in report.get("blockers") or []:
        profile = blocker.get("profile_id", "unknown")
        missing = blocker.get("missing_capabilities") or []
        errors.append(
            "[execution-capability] 研究需求命中未注册执行能力 "
            f"{profile!r}；模块三当前没有可审计的执行/评估通道。"
        )
        errors.extend(f"[execution-capability] 缺失：{item}" for item in missing)
    return errors


def _load_contract() -> dict[str, Any]:
    try:
        payload = json.loads(_CAPABILITIES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取模块三执行能力契约: {_CAPABILITIES_PATH}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("unavailable_requirement_profiles"), list):
        raise RuntimeError(f"模块三执行能力契约格式无效: {_CAPABILITIES_PATH}")
    return payload


def _requirement_text(inputs: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("research_question", "hypotheses", "gap_report", "research_frontier"):
        value = inputs.get(key)
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, (dict, list)):
            pieces.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return "\n".join(pieces)


def _profile_matches(profile: dict[str, Any], corpus: str) -> bool:
    groups = profile.get("all_signal_groups")
    if not isinstance(groups, list) or not groups:
        return False
    for group in groups:
        if not isinstance(group, list) or not any(
            isinstance(signal, str) and signal.casefold() in corpus
            for signal in group
        ):
            return False
    return True
