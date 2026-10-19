"""Aggregate Symphony runtime events into a dynamic graph overlay."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from jiuwenswarm.symphony.evolution.models import (
    ATTRIBUTION_ALL_EDGES,
    ATTRIBUTION_EXPLICIT,
    ATTRIBUTION_SUCCESS_ONLY,
    ATTRIBUTION_TERMINAL_EDGE,
    CAN_FEED,
    OUTCOME_FAILURE,
    OUTCOME_NEEDS_INPUT,
    OUTCOME_NO_PLAN,
    OUTCOME_SUCCESS,
    OVERLAY_SCHEMA_VERSION,
    PLAN_CREATED,
    PLAN_OUTCOME,
    edge_key,
    normalize_edges,
    normalize_failure_attribution,
    normalize_outcome,
    skill_id,
)

RUNTIME_WEIGHT_POLICY = "skillgraph_linear_v1"
RUNTIME_WEIGHT_STEP = 0.05
RUNTIME_WEIGHT_MIN = 0.2
RUNTIME_WEIGHT_MAX = 2.0


def build_overlay_from_events(
    events: list[dict[str, Any]],
    *,
    base_score_version: str | None = None,
) -> dict[str, Any]:
    """Build a runtime overlay from append-only evolution events."""

    generated_at = datetime.now(timezone.utc).isoformat()
    edge_stats: dict[str, dict[str, Any]] = {}
    node_stats: dict[str, dict[str, Any]] = {}
    path_stats: dict[str, dict[str, Any]] = {}
    no_plan_count = 0
    last_event_at = ""
    created_plans = _created_plan_contexts(events)

    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "")
        ts = str(event.get("ts") or "")
        if ts:
            last_event_at = max(last_event_at, ts)
        if event_type != PLAN_OUTCOME:
            continue

        outcome = normalize_outcome(event.get("outcome") or event.get("status"))
        selected_edges = normalize_edges(
            event.get("selected_edges") or event.get("can_feed_edges") or []
        )
        plan_context = created_plans.get(str(event.get("plan_id") or ""), {})
        selected_skill_ids = _selected_skill_ids(
            event,
            selected_edges,
            fallback=plan_context.get("selected_skill_ids") or [],
        )
        query = str(event.get("query") or plan_context.get("query") or "").strip()
        if selected_skill_ids and outcome != OUTCOME_NO_PLAN:
            _record_path_outcome(
                path_stats,
                event=event,
                outcome=outcome,
                query=query,
                selected_skill_ids=selected_skill_ids,
                selected_edges=selected_edges,
            )
        if outcome == OUTCOME_NO_PLAN:
            no_plan_count += 1

        for index, edge in enumerate(selected_edges):
            edge_outcome = _edge_outcome(
                event_outcome=outcome,
                edge=edge,
                index=index,
                edge_count=len(selected_edges),
                event=event,
            )
            if edge_outcome is None:
                continue
            current_key = edge_key(
                edge["source_id"],
                edge["target_id"],
                edge.get("relation_type") or CAN_FEED,
            )
            stats = edge_stats.setdefault(
                current_key,
                {
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "relation_type": edge.get("relation_type") or CAN_FEED,
                    "success_count": 0,
                    "failure_count": 0,
                    "needs_input_count": 0,
                    "attempt_count": 0,
                    "representative_queries": [],
                    "last_attribution": "",
                    "last_outcome": "",
                    "last_failure_type": "",
                    "last_updated_at": "",
                },
            )
            _apply_edge_outcome(stats, edge_outcome, event, edge)
            _apply_node_outcome(node_stats, edge["source_id"], edge_outcome, ts)
            _apply_node_outcome(node_stats, edge["target_id"], edge_outcome, ts)

    edges = {
        current_key: _finalize_edge_stats(stats)
        for current_key, stats in sorted(edge_stats.items())
    }
    nodes = {
        current_key: _finalize_node_stats(stats)
        for current_key, stats in sorted(node_stats.items())
    }
    paths = {
        current_key: _finalize_path_stats(stats)
        for current_key, stats in sorted(path_stats.items())
    }
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "base_score_version": base_score_version or "",
        "generated_at": generated_at,
        "events": {
            "count": len(events),
            "last_event_at": last_event_at,
        },
        "weight_policy": {
            "name": RUNTIME_WEIGHT_POLICY,
            "step": RUNTIME_WEIGHT_STEP,
            "minimum": RUNTIME_WEIGHT_MIN,
            "neutral": 1.0,
            "maximum": RUNTIME_WEIGHT_MAX,
            "needs_input_effect": "neutral",
        },
        "stats": {
            "edge_count": len(edges),
            "node_count": len(nodes),
            "path_count": len(paths),
            "no_plan_count": no_plan_count,
        },
        "edges": edges,
        "nodes": nodes,
        "paths": paths,
    }


def _created_plan_contexts(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contexts = {}
    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "")
        plan_id = str(event.get("plan_id") or "").strip()
        if event_type != PLAN_CREATED or not plan_id:
            continue
        selected_edges = normalize_edges(
            event.get("selected_edges") or event.get("can_feed_edges") or []
        )
        contexts[plan_id] = {
            "query": str(event.get("query") or "").strip(),
            "selected_skill_ids": _selected_skill_ids(event, selected_edges),
        }
    return contexts


def _selected_skill_ids(
    event: dict[str, Any],
    selected_edges: list[dict[str, Any]],
    *,
    fallback: list[str] | tuple[str, ...] = (),
) -> list[str]:
    values = _string_list(
        event.get("selected_skill_ids")
        or event.get("selected_skills")
        or event.get("skill_ids")
        or []
    )
    if not values:
        values = _string_list(fallback)
    if not values:
        for edge in selected_edges:
            values.extend((edge["source_id"], edge["target_id"]))
    output = []
    seen = set()
    for value in values:
        current_skill_id = skill_id(value).strip()
        if current_skill_id and current_skill_id not in seen:
            seen.add(current_skill_id)
            output.append(current_skill_id)
    return output


def _record_path_outcome(
    paths: dict[str, dict[str, Any]],
    *,
    event: dict[str, Any],
    outcome: str,
    query: str,
    selected_skill_ids: list[str],
    selected_edges: list[dict[str, Any]],
) -> None:
    canonical_edges = sorted(
        (
            {
                "source_id": edge["source_id"],
                "target_id": edge["target_id"],
                "relation_type": edge.get("relation_type") or CAN_FEED,
            }
            for edge in selected_edges
        ),
        key=lambda edge: (
            edge["source_id"],
            edge["target_id"],
            edge["relation_type"],
        ),
    )
    canonical_skills = sorted(set(selected_skill_ids))
    signature_payload = {
        "selected_skill_ids": canonical_skills,
        "can_feed_edges": canonical_edges,
    }
    digest = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    path_id = f"path:{digest}"
    stats = paths.setdefault(
        path_id,
        {
            "path_id": path_id,
            "selected_skill_ids": canonical_skills,
            "can_feed_edges": canonical_edges,
            "success_count": 0,
            "failure_count": 0,
            "needs_input_count": 0,
            "attempt_count": 0,
            "representative_queries": [],
            "example_plan_ids": [],
            "last_outcome": "",
            "last_updated_at": "",
        },
    )
    stats["attempt_count"] += 1
    if outcome == OUTCOME_SUCCESS:
        stats["success_count"] += 1
    elif outcome == OUTCOME_NEEDS_INPUT:
        stats["needs_input_count"] += 1
    else:
        stats["failure_count"] += 1
    if query and query not in stats["representative_queries"]:
        if len(stats["representative_queries"]) < 8:
            stats["representative_queries"].append(query)
    plan_id = str(event.get("plan_id") or "").strip()
    if plan_id and plan_id not in stats["example_plan_ids"]:
        if len(stats["example_plan_ids"]) < 8:
            stats["example_plan_ids"].append(plan_id)
    stats["last_outcome"] = outcome
    ts = str(event.get("ts") or "")
    if ts:
        stats["last_updated_at"] = ts


def _apply_edge_outcome(
    stats: dict[str, Any],
    outcome: str,
    event: dict[str, Any],
    edge: dict[str, Any],
) -> None:
    stats["attempt_count"] = int(stats.get("attempt_count") or 0) + 1
    if outcome == OUTCOME_SUCCESS:
        stats["success_count"] = int(stats.get("success_count") or 0) + 1
    elif outcome == OUTCOME_NEEDS_INPUT:
        stats["needs_input_count"] = int(stats.get("needs_input_count") or 0) + 1
    else:
        stats["failure_count"] = int(stats.get("failure_count") or 0) + 1
    stats["last_outcome"] = outcome
    query = str(event.get("query") or "").strip()
    representative_queries = stats.setdefault("representative_queries", [])
    if query and query not in representative_queries and len(representative_queries) < 8:
        representative_queries.append(query)
    attribution = str(event.get("failure_attribution") or "").strip()
    if attribution:
        stats["last_attribution"] = attribution
    failure_type = str(
        edge.get("failure_type")
        or event.get("failure_type")
        or event.get("error_type")
        or ""
    ).strip()
    if failure_type:
        stats["last_failure_type"] = failure_type
    ts = str(event.get("ts") or "")
    if ts:
        stats["last_updated_at"] = ts


def _apply_node_outcome(
    node_stats: dict[str, dict[str, Any]],
    node_id: str,
    outcome: str,
    ts: str,
) -> None:
    stats = node_stats.setdefault(
        node_id,
        {
            "skill_id": node_id,
            "success_count": 0,
            "failure_count": 0,
            "needs_input_count": 0,
            "attempt_count": 0,
            "last_outcome": "",
            "last_updated_at": "",
        },
    )
    stats["attempt_count"] = int(stats.get("attempt_count") or 0) + 1
    if outcome == OUTCOME_SUCCESS:
        stats["success_count"] = int(stats.get("success_count") or 0) + 1
    elif outcome == OUTCOME_NEEDS_INPUT:
        stats["needs_input_count"] = int(stats.get("needs_input_count") or 0) + 1
    else:
        stats["failure_count"] = int(stats.get("failure_count") or 0) + 1
    stats["last_outcome"] = outcome
    if ts:
        stats["last_updated_at"] = ts


def _finalize_edge_stats(stats: dict[str, Any]) -> dict[str, Any]:
    success_count = int(stats.get("success_count") or 0)
    failure_count = int(stats.get("failure_count") or 0)
    needs_input_count = int(stats.get("needs_input_count") or 0)
    attempt_count = max(
        int(stats.get("attempt_count") or 0),
        success_count + failure_count + needs_input_count,
    )
    success_rate = (success_count + 1) / (attempt_count + 2)
    reliability = (success_count + 1) / (success_count + failure_count + 2)
    runtime_weight = _runtime_weight(success_count, failure_count, needs_input_count)
    return {
        **stats,
        "attempt_count": attempt_count,
        "success_rate": round(success_rate, 4),
        "reliability": round(reliability, 4),
        "runtime_weight": round(runtime_weight, 4),
        "confidence_delta": round(runtime_weight - 1.0, 4),
    }


def _finalize_node_stats(stats: dict[str, Any]) -> dict[str, Any]:
    success_count = int(stats.get("success_count") or 0)
    failure_count = int(stats.get("failure_count") or 0)
    needs_input_count = int(stats.get("needs_input_count") or 0)
    attempt_count = max(
        int(stats.get("attempt_count") or 0),
        success_count + failure_count + needs_input_count,
    )
    success_rate = (success_count + 1) / (attempt_count + 2)
    return {
        **stats,
        "attempt_count": attempt_count,
        "success_rate": round(success_rate, 4),
    }


def _finalize_path_stats(stats: dict[str, Any]) -> dict[str, Any]:
    success_count = int(stats.get("success_count") or 0)
    failure_count = int(stats.get("failure_count") or 0)
    needs_input_count = int(stats.get("needs_input_count") or 0)
    attempt_count = max(
        int(stats.get("attempt_count") or 0),
        success_count + failure_count + needs_input_count,
    )
    return {
        **stats,
        "attempt_count": attempt_count,
        "success_rate": round((success_count + 1) / (attempt_count + 2), 4),
        "reliability": round(
            (success_count + 1) / (success_count + failure_count + 2),
            4,
        ),
    }


def _runtime_weight(
    success_count: int,
    failure_count: int,
    needs_input_count: int,
) -> float:
    """Return a bounded additive weight aligned with SkillGraph reinforcement.

    SkillGraph reinforces a successful path with a fixed additive step. Symphony
    applies the same public step size symmetrically to explicitly attributed
    failures. Missing user input is recorded, but is not evidence that an edge is
    invalid and therefore does not change its runtime weight.
    """

    del needs_input_count
    evidence = max(0, success_count) - max(0, failure_count)
    weight = 1.0 + RUNTIME_WEIGHT_STEP * evidence
    return max(RUNTIME_WEIGHT_MIN, min(RUNTIME_WEIGHT_MAX, weight))


def _edge_outcome(
    *,
    event_outcome: str,
    edge: dict[str, Any],
    index: int,
    edge_count: int,
    event: dict[str, Any],
) -> str | None:
    explicit_outcome = edge.get("outcome") or edge.get("status")
    if explicit_outcome:
        return normalize_outcome(explicit_outcome)
    if event_outcome == OUTCOME_SUCCESS:
        return OUTCOME_SUCCESS
    if event_outcome == OUTCOME_NO_PLAN:
        return None
    if event_outcome == OUTCOME_NEEDS_INPUT:
        attribution = normalize_failure_attribution(
            event.get("needs_input_attribution") or event.get("failure_attribution")
        )
        if attribution == ATTRIBUTION_ALL_EDGES:
            return OUTCOME_NEEDS_INPUT
        if attribution == ATTRIBUTION_SUCCESS_ONLY:
            return OUTCOME_SUCCESS
        if attribution == ATTRIBUTION_EXPLICIT:
            return OUTCOME_NEEDS_INPUT if _edge_marked_failed(edge) else OUTCOME_SUCCESS
        return OUTCOME_NEEDS_INPUT if index == edge_count - 1 else OUTCOME_SUCCESS
    attribution = normalize_failure_attribution(event.get("failure_attribution"))
    if attribution == ATTRIBUTION_ALL_EDGES:
        return OUTCOME_FAILURE
    if attribution == ATTRIBUTION_SUCCESS_ONLY:
        return OUTCOME_SUCCESS
    if attribution == ATTRIBUTION_EXPLICIT:
        return OUTCOME_FAILURE if _edge_marked_failed(edge) else OUTCOME_SUCCESS
    if attribution == ATTRIBUTION_TERMINAL_EDGE:
        return OUTCOME_FAILURE if index == edge_count - 1 else OUTCOME_SUCCESS
    return OUTCOME_FAILURE


def _edge_marked_failed(edge: dict[str, Any]) -> bool:
    value = edge.get("failed")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "failed", "failure"}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
