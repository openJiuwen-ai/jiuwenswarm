"""Transport-neutral evolution approval payload predicates."""

from __future__ import annotations

from typing import Any

SKILL_EVOLUTION_APPROVAL_SCHEMA = "openjiuwen.skill_evolution_approval.v1"
SKILL_EVOLUTION_APPROVAL_SOURCE = "skill_evolution_approval"


def is_evolution_approval_request_id(request_id: Any) -> bool:
    return isinstance(request_id, str) and (
        request_id.startswith("skill_evolve_")
        or request_id.startswith("team_skill_evolve_")
    )


def is_evolution_approval_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if is_evolution_approval_request_id(payload.get("request_id")):
        return True
    source = payload.get("source")
    # Payloads originate at channel boundaries and may be malformed.  Keep the
    # legacy equality semantics for non-string JSON values instead of hashing
    # them in a set (lists/dicts are unhashable and must simply not match).
    if isinstance(source, str) and source in {
        "evolution_interrupt",
        SKILL_EVOLUTION_APPROVAL_SOURCE,
    }:
        return True
    if payload.get("approval_schema") == SKILL_EVOLUTION_APPROVAL_SCHEMA:
        return True
    evolution_meta = payload.get("evolution_meta")
    return (
        isinstance(evolution_meta, dict)
        and evolution_meta.get("event_kind") == "approval"
    )


def is_interrupt_evolution_approval_answer_payload(payload: Any) -> bool:
    if not is_evolution_approval_payload(payload):
        return False
    if str(payload.get("request_id") or "").startswith("call_"):
        return True
    if payload.get("source") == "evolution_interrupt":
        return True
    evolution_meta = payload.get("evolution_meta")
    return (
        isinstance(evolution_meta, dict)
        and evolution_meta.get("approval_transport") == "interrupt"
    )


def ensure_regular_evolution_approval_metadata(
    payload: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["source"] = SKILL_EVOLUTION_APPROVAL_SOURCE
    enriched.setdefault("approval_schema", SKILL_EVOLUTION_APPROVAL_SCHEMA)
    evolution_meta = enriched.get("evolution_meta")
    if not isinstance(evolution_meta, dict):
        evolution_meta = {}
    evolution_meta = dict(evolution_meta)
    evolution_meta.setdefault("event_kind", "approval")
    evolution_meta.setdefault("rail_kind", "regular")
    evolution_meta.setdefault("approval_kind", "evolve")
    enriched["evolution_meta"] = evolution_meta
    return enriched


__all__ = [
    "SKILL_EVOLUTION_APPROVAL_SCHEMA",
    "SKILL_EVOLUTION_APPROVAL_SOURCE",
    "ensure_regular_evolution_approval_metadata",
    "is_evolution_approval_payload",
    "is_evolution_approval_request_id",
    "is_interrupt_evolution_approval_answer_payload",
]
