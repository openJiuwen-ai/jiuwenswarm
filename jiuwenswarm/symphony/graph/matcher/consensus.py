"""Consensus merging for order-swapped LLM relation matching."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from jiuwenswarm.symphony.graph.models import GraphDiagnostic, LLMMatch


def consensus_matches(
    first_matches: List[LLMMatch],
    second_matches: List[LLMMatch],
) -> tuple[List[LLMMatch], List[GraphDiagnostic]]:
    first_by_key = {
        _match_key(match): match for match in first_matches if match.accepted
    }
    second_by_key = {
        _match_key(match): match for match in second_matches if match.accepted
    }
    first_all_by_key = _matches_by_key(first_matches)
    second_all_by_key = _matches_by_key(second_matches)
    first_by_candidate = _matches_by_candidate(first_matches)
    second_by_candidate = _matches_by_candidate(second_matches)
    shared_keys = sorted(set(first_by_key) & set(second_by_key))
    consensus: List[LLMMatch] = []
    diagnostics: List[GraphDiagnostic] = []

    for key in shared_keys:
        first = first_by_key[key]
        second = second_by_key[key]
        consensus.append(
            LLMMatch(
                source_id=first.source_id,
                target_id=first.target_id,
                relation_type=first.relation_type,
                confidence=min(first.confidence, second.confidence),
                method="llm_consensus_match",
                reasons=_dedupe_strings(first.reasons + second.reasons),
                supporting_fields=_merge_supporting_fields(
                    first.supporting_fields,
                    second.supporting_fields,
                ),
                candidate_id=first.candidate_id,
                accepted=True,
                raw={
                    "first": first.raw,
                    "second": second.raw,
                    "first_confidence": first.confidence,
                    "second_confidence": second.confidence,
                },
            )
        )

    for key in sorted(set(first_by_key) ^ set(second_by_key)):
        match = first_by_key.get(key) or second_by_key[key]
        present_in = "first_run" if key in first_by_key else "second_run"
        missing_from = "second_run" if present_in == "first_run" else "first_run"
        diagnostics.append(
            GraphDiagnostic(
                stage="llm_match",
                severity="info",
                code="no_consensus_match",
                message="LLM match did not appear in both order-swapped runs.",
                skill_id=match.source_id,
                details={
                    "candidate_id": match.candidate_id,
                    "source_id": match.source_id,
                    "target_id": match.target_id,
                    "relation_type": match.relation_type,
                    "present_in": present_in,
                    "missing_consensus_from": missing_from,
                    "consensus_key": _match_key_details(key),
                    "first_run": _consensus_run_details(
                        accepted_match=first_by_key.get(key),
                        same_key_matches=first_all_by_key.get(key, []),
                        same_candidate_matches=first_by_candidate.get(
                            match.candidate_id, []
                        ),
                    ),
                    "second_run": _consensus_run_details(
                        accepted_match=second_by_key.get(key),
                        same_key_matches=second_all_by_key.get(key, []),
                        same_candidate_matches=second_by_candidate.get(
                            match.candidate_id, []
                        ),
                    ),
                    "match": match.to_dict(),
                },
            )
        )

    return consensus, diagnostics


def _matches_by_key(
    matches: List[LLMMatch],
) -> Dict[tuple[str, str, str, Optional[str]], List[LLMMatch]]:
    grouped: Dict[tuple[str, str, str, Optional[str]], List[LLMMatch]] = {}
    for match in matches:
        grouped.setdefault(_match_key(match), []).append(match)
    return grouped


def _matches_by_candidate(
    matches: List[LLMMatch],
) -> Dict[Optional[str], List[LLMMatch]]:
    grouped: Dict[Optional[str], List[LLMMatch]] = {}
    for match in matches:
        grouped.setdefault(match.candidate_id, []).append(match)
    return grouped


def _consensus_run_details(
    *,
    accepted_match: Optional[LLMMatch],
    same_key_matches: List[LLMMatch],
    same_candidate_matches: List[LLMMatch],
) -> Dict[str, Any]:
    if accepted_match is not None:
        status = "accepted"
    elif same_key_matches:
        status = "same_key_not_accepted"
    elif same_candidate_matches:
        status = "same_candidate_different_match"
    else:
        status = "not_returned"

    return {
        "status": status,
        "accepted_match": (
            accepted_match.to_dict() if accepted_match is not None else None
        ),
        "same_key_matches": [match.to_dict() for match in same_key_matches],
        "same_candidate_matches": [
            match.to_dict() for match in same_candidate_matches
        ],
    }


def _match_key(match: LLMMatch) -> tuple[str, str, str, Optional[str]]:
    return (
        match.source_id,
        match.target_id,
        match.relation_type,
        match.candidate_id,
    )


def _match_key_details(
    key: tuple[str, str, str, Optional[str]]
) -> Dict[str, Optional[str]]:
    source_id, target_id, relation_type, candidate_id = key
    return {
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "candidate_id": candidate_id,
    }


def _dedupe_strings(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _merge_supporting_fields(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = value
            continue
        if isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = _dedupe_strings([str(item) for item in merged[key] + value])
    return merged
