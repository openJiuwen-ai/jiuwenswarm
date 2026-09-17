"""Convert a progressive SearchResult into a frontend-renderable skill-tree path.

The symphony retriever emits an ordered list of ``trace_events`` describing how
the agentic search walked the skill taxonomy (build a node's subtree, select
branches, descend, reduce). The jiuwenswarm tool result only renders the final
hits as markdown and drops this traversal. This helper reshapes the traversal
into a compact, JSON-serializable payload so the web UI can replay the
"技能树路径流转" inline in the conversation.

No symphony core is touched: we only read the public ``SearchResult`` fields.
"""

from __future__ import annotations

from typing import Any


def build_skill_tree_payload(
    *,
    query: str,
    result: Any,
    catalog_by_worker: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the ``skill_tree`` payload, or ``None`` when there is nothing to show."""

    trace_events = _as_list(getattr(result, "trace_events", None))
    candidate_records = _as_list(getattr(result, "candidate_records", None))
    if not trace_events and not candidate_records:
        return None

    steps = [step for step in (_step_from_event(index, event) for index, event in enumerate(trace_events)) if step]
    candidates = _build_candidates(candidate_records, catalog_by_worker)
    if not steps and not candidates:
        return None

    max_depth = max((int(step["depth"]) for step in steps), default=0)
    return {
        "query": str(query or ""),
        "elapsed_ms": _to_float(getattr(result, "elapsed_ms", None)),
        "max_depth": max_depth,
        "candidate_count": len(candidates),
        "steps": steps,
        "candidates": candidates,
    }


def _step_from_event(index: int, event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("event_type") or "").strip()
    if not event_type:
        return None
    node_id = str(event.get("node_id") or "").strip()
    depth = _to_int(event.get("depth"))
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}

    selected = _named_ids(detail.get("selected_canonical_ids") or detail.get("selected_codes"))
    leaves = _named_ids(detail.get("selected_terminal_ids"))
    branches = _named_ids(detail.get("selected_branch_ids"))

    return {
        "order": index,
        "event_type": event_type,
        "node_id": node_id,
        "label": _humanize(node_id),
        "depth": depth,
        "selectable_count": _to_int(detail.get("selectable_count")) if "selectable_count" in detail else None,
        "selected": selected,
        "branches": branches,
        "leaves": leaves,
        "candidate_count": _to_int(detail.get("candidate_count")) if "candidate_count" in detail else None,
    }


def _build_candidates(
    candidate_records: list[Any],
    catalog_by_worker: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(candidate_records, start=1):
        if not isinstance(raw, dict):
            continue
        worker_id = str(raw.get("worker_id") or raw.get("resolved_payload") or "").strip()
        catalog = catalog_by_worker.get(worker_id, {})
        name = str(raw.get("skill_name") or catalog.get("name") or worker_id or f"skill-{index}").strip()
        description = str(raw.get("description") or catalog.get("description") or "").strip()
        choice_id = str(raw.get("choice_id") or raw.get("raw_output") or "").strip()
        candidates.append(
            {
                "rank": _to_int(raw.get("rank")) or index,
                "label": name,
                "worker_id": worker_id,
                "description": _compact(description, 240),
                "path": _split_path(choice_id),
                "selected": bool(raw.get("selected")),
                "source": str(raw.get("source") or "").strip(),
            }
        )
    return candidates


def _named_ids(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in _as_list(value):
        text = str(item or "").strip()
        if text:
            out.append({"id": text, "label": _humanize(text)})
    return out


def _split_path(canonical_id: str) -> list[str]:
    return [segment for segment in str(canonical_id or "").split(".") if segment]


def _humanize(node_id: str) -> str:
    text = str(node_id or "").strip()
    if not text:
        return ""
    last = text.split(".")[-1]
    return last.replace("_", " ").replace("-", " ").strip() or text


def _compact(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
