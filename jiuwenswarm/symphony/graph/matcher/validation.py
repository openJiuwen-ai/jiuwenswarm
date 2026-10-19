"""Validate raw LLM relation match payloads."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from jiuwenswarm.symphony.graph.matcher.constants import DEFAULT_THRESHOLDS
from jiuwenswarm.symphony.graph.models import (
    ALLOWED_RELATION_TYPES,
    GraphDiagnostic,
    LLMMatch,
    RelationCandidate,
    SkillRegistry,
)


def validate_llm_matches(
    payload: Dict[str, Any],
    registry: SkillRegistry,
    candidates: Iterable[RelationCandidate],
    *,
    thresholds: Optional[Dict[str, float]] = None,
) -> tuple[List[LLMMatch], List[GraphDiagnostic]]:
    """Normalize and validate raw LLM matches."""

    thresholds = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    candidate_by_key = {candidate.key: candidate for candidate in candidates}
    candidates_by_pair = {}
    for candidate in candidates:
        candidates_by_pair[(candidate.source_id, candidate.target_id)] = candidate
        candidates_by_pair[(candidate.target_id, candidate.source_id)] = candidate
    matches: List[LLMMatch] = []
    diagnostics: List[GraphDiagnostic] = []

    raw_matches = payload.get("matches", [])
    if not isinstance(raw_matches, list):
        return [], [
            GraphDiagnostic(
                stage="llm_match",
                severity="error",
                code="invalid_matches_payload",
                message="LLM payload field 'matches' must be a list.",
            )
        ]

    for index, raw in enumerate(raw_matches):
        if not isinstance(raw, dict):
            diagnostics.append(
                GraphDiagnostic(
                    stage="llm_match",
                    severity="warning",
                    code="invalid_match_item",
                    message="LLM match item is not an object.",
                    details={"index": index, "item": raw},
                )
            )
            continue

        match, item_diagnostics = _normalize_match(
            raw, registry, candidate_by_key, candidates_by_pair, thresholds
        )
        diagnostics.extend(item_diagnostics)
        if match is not None:
            matches.append(match)

    return matches, diagnostics


def _normalize_match(
    raw: Dict[str, Any],
    registry: SkillRegistry,
    candidate_by_key: Dict[str, RelationCandidate],
    candidates_by_pair: Dict[tuple[str, str], RelationCandidate],
    thresholds: Dict[str, float],
) -> tuple[Optional[LLMMatch], List[GraphDiagnostic]]:
    diagnostics: List[GraphDiagnostic] = []
    source_id = str(raw.get("source_id") or "")
    target_id = str(raw.get("target_id") or "")
    relation_type = str(raw.get("relation_type") or "")
    candidate_id = raw.get("candidate_id")
    candidate_id = str(candidate_id) if candidate_id else None

    errors: List[str] = []
    if source_id not in registry.skills:
        errors.append("source_id does not exist")
    if target_id not in registry.skills:
        errors.append("target_id does not exist")
    if relation_type not in ALLOWED_RELATION_TYPES:
        errors.append("relation_type is not allowed")

    candidate = None
    if candidate_id:
        candidate = candidate_by_key.get(candidate_id)
    if candidate is None:
        candidate = candidates_by_pair.get((source_id, target_id))
    if candidate is None:
        errors.append("match does not correspond to an input candidate")
    elif relation_type not in candidate.relation_hints:
        errors.append("relation_type is not allowed for the input candidate")

    try:
        confidence = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
        errors.append("confidence is not numeric")
    if confidence < 0 or confidence > 1:
        errors.append("confidence must be between 0 and 1")
        confidence = max(0, min(1, confidence))

    reasons = [str(item) for item in raw.get("reasons", []) if str(item).strip()]
    supporting_fields = raw.get("supporting_fields")
    if not isinstance(supporting_fields, dict):
        supporting_fields = {}

    if relation_type == "can_feed" and candidate is not None:
        source_outputs = set(_field_names(supporting_fields.get("source_outputs", [])))
        target_inputs = set(_field_names(supporting_fields.get("target_inputs", [])))
        requested_port_mappings = _port_mapping_pairs(
            supporting_fields.get("port_mappings", [])
        )
        directional_evidence = _directional_evidence(
            candidate,
            source_id,
            target_id,
        )
        evidence_port_mappings = _port_mapping_pairs(
            directional_evidence.get("port_mappings", [])
        )
        evidence_outputs = {
            item.get("name")
            for item in directional_evidence.get("source_outputs", [])
            if isinstance(item, dict)
        }
        evidence_inputs = {
            item.get("name")
            for item in directional_evidence.get("target_inputs", [])
            if isinstance(item, dict)
        }
        if requested_port_mappings:
            if not requested_port_mappings <= evidence_port_mappings:
                errors.append("port_mappings do not match candidate evidence")
            supporting_fields = _complete_supporting_fields_from_port_mappings(
                supporting_fields,
                requested_port_mappings,
            )
        elif source_outputs and target_inputs:
            if not (source_outputs & evidence_outputs and target_inputs & evidence_inputs):
                errors.append("supporting_fields do not match candidate evidence")
        elif not ((evidence_outputs and evidence_inputs) or evidence_port_mappings):
            errors.append("can_feed has no supported output/input pair")

    accepted = not errors and confidence >= thresholds.get(relation_type, 1.0)
    match = LLMMatch(
        source_id=source_id,
        target_id=target_id,
        relation_type=relation_type,
        confidence=confidence,
        method=str(raw.get("method") or "llm_ontology_match"),
        reasons=reasons,
        supporting_fields=supporting_fields,
        candidate_id=candidate.key if candidate is not None else candidate_id,
        accepted=accepted,
        diagnostics=errors,
        raw=raw,
    )

    if errors:
        diagnostics.append(
            GraphDiagnostic(
                stage="llm_match",
                severity="warning",
                code="rejected_llm_match",
                message="LLM match failed validation.",
                skill_id=source_id or None,
                details={"errors": errors, "match": raw},
            )
        )
    elif not accepted:
        diagnostics.append(
            GraphDiagnostic(
                stage="llm_match",
                severity="info",
                code="low_confidence_llm_match",
                message="LLM match is below the relation threshold.",
                skill_id=source_id,
                details={
                    "threshold": thresholds.get(relation_type),
                    "match": match.to_dict(),
                },
            )
        )

    return match, diagnostics


def _field_names(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    names = []
    for value in values:
        if isinstance(value, str):
            names.append(_field_name_from_string(value))
        elif isinstance(value, dict) and value.get("name"):
            names.append(str(value["name"]))
    return [name for name in names if name]


def _field_name_from_string(value: str) -> str:
    text = str(value).strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip()
    head = head.split("(", 1)[0].strip()
    return head


def _port_mapping_pairs(values: Any) -> set[tuple[str, str]]:
    if not isinstance(values, list):
        return set()
    pairs = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        source_output = str(value.get("source_output") or "").strip()
        target_input = str(value.get("target_input") or "").strip()
        if source_output and target_input:
            pairs.add((source_output, target_input))
    return pairs


def _complete_supporting_fields_from_port_mappings(
    supporting_fields: Dict[str, Any],
    pairs: set[tuple[str, str]],
) -> Dict[str, Any]:
    completed = dict(supporting_fields)
    completed.setdefault("source_outputs", sorted({source for source, _ in pairs}))
    completed.setdefault("target_inputs", sorted({target for _, target in pairs}))
    return completed


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
    return candidate.evidence
