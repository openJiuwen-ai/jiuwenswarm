"""Shared helpers for Symphony evolution events and overlay keys."""

from __future__ import annotations

from typing import Any

OVERLAY_SCHEMA_VERSION = "symphony.dynamic_overlay.v3"
EVENT_SCHEMA_VERSION = "symphony.evolution_event.v2"

PLAN_CREATED = "plan.created"
PLAN_OUTCOME = "plan.outcome"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_NO_PLAN = "no_plan"
OUTCOME_NEEDS_INPUT = "needs_input"

CAN_FEED = "can_feed"

ATTRIBUTION_ALL_EDGES = "all_edges"
ATTRIBUTION_TERMINAL_EDGE = "terminal_edge"
ATTRIBUTION_EXPLICIT = "explicit"
ATTRIBUTION_SUCCESS_ONLY = "success_only"

ALLOWED_FAILURE_ATTRIBUTIONS = {
    ATTRIBUTION_ALL_EDGES,
    ATTRIBUTION_TERMINAL_EDGE,
    ATTRIBUTION_EXPLICIT,
    ATTRIBUTION_SUCCESS_ONLY,
}


def edge_key(source: Any, target: Any, relation_type: str = CAN_FEED) -> str:
    """Return a stable key for a runtime edge overlay entry."""

    return f"{skill_id(source)}->{skill_id(target)}:{relation_type or CAN_FEED}"


def skill_id(node_id: Any) -> str:
    text = str(node_id or "")
    return text.removeprefix("skill:")


def normalize_edge(item: Any) -> dict[str, Any] | None:
    """Normalize any edge-like payload to source_id/target_id/relation_type."""

    if not isinstance(item, dict):
        return None
    source = skill_id(item.get("source_id") or item.get("source")).strip()
    target = skill_id(item.get("target_id") or item.get("target")).strip()
    if not source or not target:
        return None
    relation_type = str(
        item.get("relation_type") or item.get("type") or CAN_FEED
    ).strip() or CAN_FEED
    return {
        "source_id": source,
        "target_id": target,
        "relation_type": relation_type,
        "confidence": item.get("confidence"),
        "outcome": item.get("outcome") or item.get("status"),
        "failure_type": item.get("failure_type") or item.get("error_type"),
        "failed": item.get("failed"),
        "weight": item.get("weight"),
    }


def normalize_edges(values: Any) -> list[dict[str, Any]]:
    """Normalize and deduplicate a list of edge-like payloads."""

    if not isinstance(values, list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        normalized = normalize_edge(item)
        if normalized is None:
            continue
        key = edge_key(
            normalized["source_id"],
            normalized["target_id"],
            normalized["relation_type"],
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def normalize_outcome(value: Any) -> str:
    """Normalize runtime outcome labels to the overlay vocabulary."""

    outcome = str(value or "").strip().lower()
    if outcome in {OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_NO_PLAN, OUTCOME_NEEDS_INPUT}:
        return outcome
    if outcome in {"failed", "error", "exception", "invalid"}:
        return OUTCOME_FAILURE
    if outcome in {"ready", "ok", "done", "passed"}:
        return OUTCOME_SUCCESS
    return OUTCOME_FAILURE


def normalize_failure_attribution(value: Any) -> str:
    """Normalize failure-attribution strategy labels."""

    text = str(value or "").strip().lower()
    if text in ALLOWED_FAILURE_ATTRIBUTIONS:
        return text
    if text in {"terminal", "last_edge", "last"}:
        return ATTRIBUTION_TERMINAL_EDGE
    if text in {"all", "plan", "plan_level"}:
        return ATTRIBUTION_ALL_EDGES
    if text in {"edge", "per_edge"}:
        return ATTRIBUTION_EXPLICIT
    return ATTRIBUTION_TERMINAL_EDGE
