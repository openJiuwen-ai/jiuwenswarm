"""Compact prompt protocol for LLM relation matching."""

from __future__ import annotations

from typing import Any, Dict, List

from jiuwenswarm.symphony.fingerprint.models import Fingerprint
from jiuwenswarm.symphony.graph.models import (
    GraphDiagnostic,
    RelationCandidate,
    SkillRegistry,
)
from jiuwenswarm.symphony.shared.llm_payload import prune_empty

_MAX_SKILL_DESCRIPTION_LENGTH = 240
_MAX_PORT_DESCRIPTION_LENGTH = 160
_MAX_REASON_LENGTH = 160


def build_llm_context(
    registry: SkillRegistry,
    candidates: List[RelationCandidate],
    *,
    reverse_skill_order: bool = False,
) -> Dict[str, Any]:
    indexed = [
        (f"c{index}", candidate)
        for index, candidate in enumerate(candidates, start=1)
    ]
    if reverse_skill_order:
        indexed.reverse()
    return {
        "candidates": [
            _candidate_context(candidate_id, registry, candidate)
            for candidate_id, candidate in indexed
        ]
    }


def expand_compact_llm_response(
    payload: Dict[str, Any],
    candidates: List[RelationCandidate],
) -> tuple[Dict[str, Any], List[GraphDiagnostic]]:
    candidates_by_id = {
        f"c{index}": candidate
        for index, candidate in enumerate(candidates, start=1)
    }
    matches = payload.get("matches", [])
    if not isinstance(matches, list):
        return {"matches": matches}, []

    expanded = []
    diagnostics = []
    seen = set()
    for item in matches:
        if not isinstance(item, dict):
            diagnostics.append(_protocol_diagnostic(
                "invalid_match_item",
                "Compact LLM match item must be an object.",
            ))
            continue
        candidate_id = str(item.get("id") or "").strip()
        if candidate_id not in candidates_by_id:
            diagnostics.append(_protocol_diagnostic(
                "unknown_candidate_id",
                f"Compact LLM match returned unknown candidate id: {candidate_id!r}.",
            ))
            continue
        if candidate_id in seen:
            diagnostics.append(_protocol_diagnostic(
                "duplicate_candidate_id",
                f"Compact LLM match repeated candidate id: {candidate_id!r}.",
            ))
            continue
        seen.add(candidate_id)
        candidate = candidates_by_id[candidate_id]
        direction = str(item.get("direction") or "forward").strip().lower()
        if direction not in {"forward", "reverse"}:
            diagnostics.append(_protocol_diagnostic(
                "invalid_candidate_direction",
                f"Compact LLM match returned invalid direction: {direction!r}.",
            ))
            continue
        source_id, target_id = _directed_ids(candidate, direction)
        evidence = _directional_evidence(candidate, source_id, target_id)
        if not evidence:
            diagnostics.append(_protocol_diagnostic(
                "unsupported_candidate_direction",
                f"Candidate {candidate_id!r} has no evidence for {direction}.",
            ))
            continue
        reason = str(item.get("reason") or "").strip()[:_MAX_REASON_LENGTH]
        expanded.append({
            "candidate_id": candidate.key,
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": "can_feed",
            "confidence": item.get("confidence", 0),
            "method": "llm_ontology_match",
            "reasons": [reason] if reason else [],
            "supporting_fields": _supporting_fields(evidence),
        })
    return {"matches": expanded}, diagnostics


def _candidate_context(
    candidate_id: str,
    registry: SkillRegistry,
    candidate: RelationCandidate,
) -> Dict[str, Any]:
    source = registry.skills[candidate.source_id]
    target = registry.skills[candidate.target_id]
    directions = {}
    for direction, source_id, target_id in (
        ("forward", candidate.source_id, candidate.target_id),
        ("reverse", candidate.target_id, candidate.source_id),
    ):
        evidence = _directional_evidence(candidate, source_id, target_id)
        if evidence:
            directions[direction] = _evidence_context(evidence)
    return {
        "id": candidate_id,
        "source": _skill_context(source),
        "target": _skill_context(target),
        "directions": directions,
    }


def _skill_context(skill: Fingerprint) -> Dict[str, str]:
    return prune_empty({
        "name": skill.name,
        "description": skill.description[:_MAX_SKILL_DESCRIPTION_LENGTH],
    })


def _evidence_context(evidence: Dict[str, Any]) -> Dict[str, Any]:
    outputs = _compact_fields(evidence.get("source_outputs", []))
    inputs = _compact_fields(evidence.get("target_inputs", []))
    ports = []
    seen = set()
    for mapping in evidence.get("port_mappings", []):
        if not isinstance(mapping, dict):
            continue
        port = prune_empty({
            "output": mapping.get("source_output"),
            "output_type": mapping.get("source_type"),
            "input": mapping.get("target_input"),
            "input_type": mapping.get("target_type"),
        })
        marker = tuple(sorted(port.items()))
        if not port or marker in seen:
            continue
        seen.add(marker)
        ports.append(port)
    return prune_empty({
        "outputs": outputs,
        "inputs": inputs,
        "ports": ports,
    })


def _compact_fields(values: Any) -> List[Dict[str, Any]]:
    output = []
    seen = set()
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        field = prune_empty({
            "name": item.get("name"),
            "type": item.get("type"),
            "required": item.get("required"),
            "description": str(item.get("description") or "")[
                :_MAX_PORT_DESCRIPTION_LENGTH
            ],
        })
        marker = (
            field.get("name"),
            field.get("type"),
            field.get("required"),
        )
        if marker in seen:
            continue
        seen.add(marker)
        output.append(field)
    return output


def _directional_evidence(
    candidate: RelationCandidate,
    source_id: str,
    target_id: str,
) -> Dict[str, Any]:
    directions = candidate.evidence.get("directions", {})
    if isinstance(directions, dict):
        evidence = directions.get(f"{source_id}->{target_id}")
        if isinstance(evidence, dict):
            return evidence
        if "directions" in candidate.evidence:
            return {}
    if source_id == candidate.source_id and target_id == candidate.target_id:
        return candidate.evidence
    return {}


def _directed_ids(
    candidate: RelationCandidate,
    direction: str,
) -> tuple[str, str]:
    if direction == "reverse":
        return candidate.target_id, candidate.source_id
    return candidate.source_id, candidate.target_id


def _supporting_fields(evidence: Dict[str, Any]) -> Dict[str, Any]:
    pairs = []
    source_outputs = set()
    target_inputs = set()
    for mapping in evidence.get("port_mappings", []):
        if not isinstance(mapping, dict):
            continue
        source_output = str(mapping.get("source_output") or "").strip()
        target_input = str(mapping.get("target_input") or "").strip()
        if not source_output or not target_input:
            continue
        pair = {
            "source_output": source_output,
            "target_input": target_input,
        }
        if pair not in pairs:
            pairs.append(pair)
        source_outputs.add(source_output)
        target_inputs.add(target_input)
    if not pairs:
        source_outputs.update(_field_names(evidence.get("source_outputs", [])))
        target_inputs.update(_field_names(evidence.get("target_inputs", [])))
    return prune_empty({
        "port_mappings": pairs,
        "source_outputs": sorted(source_outputs),
        "target_inputs": sorted(target_inputs),
    })


def _field_names(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    names: List[str] = []
    for item in values:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def _protocol_diagnostic(code: str, message: str) -> GraphDiagnostic:
    return GraphDiagnostic(
        stage="llm_match",
        severity="warning",
        code=code,
        message=message,
    )


SYSTEM_PROMPT = """Validate whether each candidate Skill output can feed the target input.

Return JSON only:
{"matches":[{"id":"c1","direction":"forward|reverse","confidence":0.0,"reason":"optional short reason"}]}

Return at most one judgment per candidate id. Use only directions and ports
present in the request. Omit invalid relations or assign low confidence.
Confidence must be between 0 and 1. Keep reason to one short sentence when
useful; do not repeat request data or invent Skills, ports or relations.
"""
