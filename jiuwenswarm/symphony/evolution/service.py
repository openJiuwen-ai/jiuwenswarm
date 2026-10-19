"""Service helpers for Symphony dynamic graph evolution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jiuwenswarm.symphony.evolution.aggregate import build_overlay_from_events
from jiuwenswarm.symphony.evolution.models import (
    EVENT_SCHEMA_VERSION,
    OUTCOME_NEEDS_INPUT,
    OUTCOME_FAILURE,
    PLAN_OUTCOME,
    edge_key,
    normalize_edges,
    normalize_failure_attribution,
    normalize_outcome,
    skill_id,
)
from jiuwenswarm.symphony.evolution.store import (
    append_event,
    evolution_store_transaction,
    events_path,
    overlay_file_mtime,
    overlay_path,
    read_events,
    read_overlay,
    write_overlay,
)


def load_dynamic_overlay(score_dir: str | Path) -> dict[str, Any] | None:
    """Load the current dynamic overlay if it exists."""

    return read_overlay(score_dir)


def rebuild_dynamic_overlay(
    score_dir: str | Path,
    *,
    base_score_version: str | None = None,
) -> dict[str, Any]:
    """Rebuild and persist the dynamic overlay from event logs."""

    with evolution_store_transaction():
        events = read_events(score_dir)
        overlay = build_overlay_from_events(
            events,
            base_score_version=base_score_version,
        )
        write_overlay(score_dir, overlay)
    return overlay


def evolution_status(score_dir: str | Path) -> dict[str, Any]:
    """Return a compact runtime-evolution status payload."""

    events = read_events(score_dir)
    overlay = read_overlay(score_dir)
    overlay_mtime = overlay_file_mtime(score_dir)
    stats = overlay.get("stats") if isinstance(overlay, dict) else {}
    payload = {
        "success": True,
        "score_dir": str(score_dir),
        "event_log": str(events_path(score_dir)),
        "overlay_path": str(overlay_path(score_dir)),
        "event_count": len(events),
        "last_event_at": _last_event_ts(events),
        "overlay_exists": overlay is not None,
        "overlay_mtime": overlay_mtime,
        "overlay_generated_at": overlay.get("generated_at") if isinstance(overlay, dict) else "",
        "overlay_stats": stats if isinstance(stats, dict) else {},
        "weight_policy": (
            dict(overlay.get("weight_policy") or {}) if isinstance(overlay, dict) else {}
        ),
        "top_edges": _top_overlay_edges(overlay),
    }
    try:
        from jiuwenswarm.symphony.evolution.session_consumer import (
            session_feedback_status,
        )

        payload["session_feedback"] = session_feedback_status(score_dir)
    except Exception:  # noqa: BLE001
        payload["session_feedback"] = {
            "source": "session_history",
            "available": False,
        }
    return payload


def record_plan_outcome(
    score_dir: str | Path,
    *,
    plan_id: str,
    outcome: str,
    query: str = "",
    selected_skill_ids: list[str] | None = None,
    selected_edges: list[dict[str, Any]] | None = None,
    failed_edges: list[dict[str, Any]] | None = None,
    missing_inputs: list[dict[str, Any]] | None = None,
    failure_attribution: str = "",
    failure_type: str = "",
    detail: str = "",
    source: str = "",
    evidence_id: str = "",
    session_id: str = "",
    request_id: str = "",
    evidence: dict[str, Any] | None = None,
    rebuild_overlay: bool = True,
) -> dict[str, Any]:
    """Append a plan outcome event and optionally refresh the overlay."""

    normalized_outcome = normalize_outcome(outcome)
    normalized_edges = _annotate_failed_edges(
        normalize_edges(selected_edges or []),
        failed_edges=failed_edges or [],
    )
    attribution = _default_failure_attribution(
        normalized_outcome,
        normalized_edges,
        failure_attribution,
    )
    event = _base_event(PLAN_OUTCOME, plan_id=plan_id, query=query)
    event.update(
        {
            "outcome": normalized_outcome,
            "failure_type": str(failure_type or "").strip(),
            "failure_attribution": attribution,
            "detail": str(detail or "").strip()[:1000],
            "selected_skill_ids": _clean_skill_ids(selected_skill_ids or []),
            "selected_edges": normalized_edges,
            "missing_inputs": [
                item for item in (missing_inputs or []) if isinstance(item, dict)
            ],
        }
    )
    clean_source = str(source or "").strip()
    clean_evidence_id = str(evidence_id or "").strip()
    if clean_source:
        event["source"] = clean_source
    if clean_evidence_id:
        event["evidence_id"] = clean_evidence_id
    if session_id:
        event["session_id"] = str(session_id).strip()
    if request_id:
        event["request_id"] = str(request_id).strip()
    if isinstance(evidence, dict) and evidence:
        event["evidence"] = dict(evidence)

    with evolution_store_transaction():
        if clean_evidence_id:
            existing = next(
                (
                    item
                    for item in read_events(score_dir)
                    if str(item.get("evidence_id") or "") == clean_evidence_id
                ),
                None,
            )
            if existing is not None:
                return {**existing, "deduplicated": True}
        append_event(score_dir, event)
        if rebuild_overlay:
            rebuild_dynamic_overlay(score_dir)
    return event


def _base_event(event_type: str, *, plan_id: str, query: str) -> dict[str, Any]:
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": str(uuid4()),
        "event_type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "query": str(query or "").strip(),
    }


def _last_event_ts(events: list[dict[str, Any]]) -> str:
    return max((str(event.get("ts") or "") for event in events), default="")


def _default_failure_attribution(
    outcome: str,
    selected_edges: list[dict[str, Any]],
    requested: str,
) -> str:
    if requested:
        return normalize_failure_attribution(requested)
    if outcome in {OUTCOME_FAILURE, OUTCOME_NEEDS_INPUT} and len(selected_edges) > 1:
        return "terminal_edge"
    if outcome in {OUTCOME_FAILURE, OUTCOME_NEEDS_INPUT}:
        return "all_edges"
    return ""


def _annotate_failed_edges(
    selected_edges: list[dict[str, Any]],
    *,
    failed_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    failed_keys = set()
    for edge in normalize_edges(failed_edges):
        failed_keys.add(
            edge_key(
                edge.get("source_id") or edge.get("source"),
                edge.get("target_id") or edge.get("target"),
                edge.get("relation_type") or edge.get("type") or "can_feed",
            )
        )
    if not failed_keys:
        return selected_edges
    output = []
    for edge in selected_edges:
        item = dict(edge)
        current_key = edge_key(
            item.get("source_id"),
            item.get("target_id"),
            item.get("relation_type") or "can_feed",
        )
        if current_key in failed_keys:
            item["failed"] = True
        output.append(item)
    return output


def _clean_skill_ids(values: list[Any]) -> list[str]:
    output = []
    seen = set()
    for value in values:
        current_skill_id = skill_id(value).strip()
        if current_skill_id and current_skill_id not in seen:
            seen.add(current_skill_id)
            output.append(current_skill_id)
    return output


def _top_overlay_edges(overlay: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(overlay, dict):
        return []
    edges = overlay.get("edges")
    if not isinstance(edges, dict):
        return []
    items = [
        {"edge_key": key, **value}
        for key, value in edges.items()
        if isinstance(value, dict)
    ]
    return sorted(
        items,
        key=lambda item: (
            -float(item.get("runtime_weight") or 1.0),
            -int(item.get("attempt_count") or 0),
            str(item.get("edge_key") or ""),
        ),
    )[:10]
