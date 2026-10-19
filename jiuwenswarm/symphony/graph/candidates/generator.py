"""Deterministic high-recall relation candidate generation."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, Iterable, List, MutableMapping, Set, Tuple

from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ParameterSpec,
    Fingerprint,
)
from jiuwenswarm.symphony.fingerprint.normalize.data_type_vocab import (
    DataTypeVocabulary,
)
from jiuwenswarm.symphony.graph.lexicon import (
    ArtifactLexicon,
    DEFAULT_GRAPH_STOP_TERMS,
)
from jiuwenswarm.symphony.graph.models import (
    ALLOWED_RELATION_TYPES,
    RelationCandidate,
    SkillRegistry,
)


PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}
# These names are too broad for exact-name edge generation. They are still
# available for text indexing and semantic evidence; only exact I/O matching
# treats them as generic.
IO_NAMES_EXCLUDED_FROM_EDGE_BUILDING = frozenset(
    {
        "dependencies",
        "path",
    }
)
# Textual coercion is intentionally limited to inputs whose names clearly mean
# "free-form content". This is a graph-generation heuristic, not a DataType.
GENERIC_CONTENT_INPUT_NAMES = frozenset(
    {
        "body",
        "content",
        "prompt",
        "query",
        "question",
        "text",
        "sources",
    }
)
_GRAPH_TERM_LEXICON = ArtifactLexicon.create(
    stop_terms=DEFAULT_GRAPH_STOP_TERMS,
    min_token_length=3,
)
LOGGER = logging.getLogger("jiuwenswarm.symphony.graph.candidates")


class CandidateGenerator:
    """Generate high-recall Skill-Skill relation candidates."""

    def __init__(
        self,
        *,
        max_candidates_per_skill_relation: int = 32,
        max_port_mappings_per_candidate: int = 12,
        generic_io_names: Iterable[str] = IO_NAMES_EXCLUDED_FROM_EDGE_BUILDING,
        max_exact_io_pair_fanout: int = 64,
    ) -> None:
        self.max_candidates_per_skill_relation = max_candidates_per_skill_relation
        self.max_port_mappings_per_candidate = max(1, max_port_mappings_per_candidate)
        self.evidence_merger = CandidateEvidenceMerger(
            max_port_mappings=self.max_port_mappings_per_candidate,
        )
        self.lexicon = ArtifactLexicon.create(
            stop_terms=DEFAULT_GRAPH_STOP_TERMS,
            min_token_length=3,
            generic_io_names=generic_io_names,
        )
        self.max_exact_io_pair_fanout = max(1, max_exact_io_pair_fanout)

    def generate(self, registry: SkillRegistry) -> List[RelationCandidate]:
        skills = registry.ordered_skills()
        skill_terms = {skill.id: _skill_terms(skill) for skill in skills}
        indexes = CandidateIndexes.from_skills(skills)
        candidates: Dict[Tuple[str, str], RelationCandidate] = {}

        LOGGER.debug("candidate_generation_start skill_count=%s", len(skills))
        self._add_exact_io_candidates(indexes, candidates)
        self._add_semantic_overlap_candidates(skills, skill_terms, candidates)
        self._add_textual_coercion_candidates(skills, skill_terms, candidates)

        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                item.source_id,
                -PRIORITY_RANK.get(item.priority, 0),
                item.target_id,
                ",".join(item.relation_hints),
            ),
        )
        limited = self._limit_per_skill_relation(ordered)
        LOGGER.debug(
            "candidate_generation_done generated_count=%s emitted_count=%s",
            len(ordered),
            len(limited),
        )
        return limited

    def _add_exact_io_candidates(
        self,
        indexes: "CandidateIndexes",
        candidates: MutableMapping[Tuple[str, str], RelationCandidate],
    ) -> None:
        for name in sorted(set(indexes.by_output_name) & set(indexes.by_input_name)):
            if self.lexicon.is_generic_io_name(name):
                LOGGER.debug(
                    "candidate_skipped reason=generic_io_name "
                    "method=exact_io_match name=%s output_skills=%s input_skills=%s",
                    name,
                    ",".join(sorted(indexes.by_output_name[name])),
                    ",".join(sorted(indexes.by_input_name[name])),
                )
                continue
            pair_fanout = (
                len(indexes.by_output_name[name]) * len(indexes.by_input_name[name])
            )
            if pair_fanout > self.max_exact_io_pair_fanout:
                LOGGER.debug(
                    "candidate_skipped reason=max_exact_io_pair_fanout "
                    "method=exact_io_match name=%s fanout=%s limit=%s "
                    "output_skills=%s input_skills=%s",
                    name,
                    pair_fanout,
                    self.max_exact_io_pair_fanout,
                    ",".join(sorted(indexes.by_output_name[name])),
                    ",".join(sorted(indexes.by_input_name[name])),
                )
                continue
            for source_id in sorted(indexes.by_output_name[name]):
                for target_id in sorted(indexes.by_input_name[name]):
                    if source_id == target_id:
                        continue
                    source_outputs = indexes.outputs_by_skill_name[(source_id, name)]
                    target_inputs = indexes.inputs_by_skill_name[(target_id, name)]
                    port_mappings = []
                    for output in source_outputs:
                        for parameter in target_inputs:
                            if not _can_feed_by_exact_or_subtype(
                                output.type,
                                parameter.type,
                            ):
                                continue
                            port_mappings.append(
                                _port_mapping(
                                    output,
                                    parameter,
                                    match_method="exact_io_match",
                                    match_reason=_match_reason_exact_io(
                                        output.type,
                                        parameter.type,
                                    ),
                                )
                            )
                    if not port_mappings:
                        continue
                    self._merge_candidate(
                        candidates,
                        RelationCandidate(
                            source_id=source_id,
                            target_id=target_id,
                            relation_hints=["can_feed"],
                            candidate_methods=["exact_io_match"],
                            priority="high",
                            evidence={
                                "matched_terms": [name],
                                "source_outputs": [
                                    item.to_dict() for item in source_outputs
                                ],
                                "target_inputs": [
                                    item.to_dict() for item in target_inputs
                                ],
                                "port_mappings": port_mappings,
                            },
                        ),
                    )

    def _add_semantic_overlap_candidates(
        self,
        skills: List[Fingerprint],
        skill_terms: Dict[str, Set[str]],
        candidates: MutableMapping[Tuple[str, str], RelationCandidate],
    ) -> None:
        for source in skills:
            source_terms = skill_terms[source.id]
            for target in skills:
                if source.id == target.id:
                    continue
                target_terms = skill_terms[target.id]
                shared_terms = sorted(source_terms & target_terms)
                for output in source.outputs:
                    for parameter in target.inputs:
                        if (
                            output.name == parameter.name
                            and self.lexicon.is_generic_io_name(output.name)
                        ):
                            continue
                        uses_remote_reference = _can_feed_by_remote_reference(
                            output,
                            parameter,
                        )
                        if not uses_remote_reference:
                            if not _can_feed_by_type(output.type, parameter.type):
                                continue
                            if not _has_semantic_overlap(output, parameter):
                                continue
                        mapping = _port_mapping(
                            output,
                            parameter,
                            match_method="semantic_overlap_match",
                            match_reason=(
                                _match_reason_remote_reference(output, parameter)
                                if uses_remote_reference
                                else _match_reason_semantic_overlap(output, parameter)
                            ),
                        )
                        self._merge_candidate(
                            candidates,
                            RelationCandidate(
                                source_id=source.id,
                                target_id=target.id,
                                relation_hints=["can_feed"],
                                candidate_methods=["semantic_overlap_match"],
                                priority="medium",
                                evidence={
                                    "matched_terms": shared_terms,
                                    "source_outputs": [output.to_dict()],
                                    "target_inputs": [parameter.to_dict()],
                                    "port_mappings": [mapping],
                                    "matched_types": [
                                        f"{output.type}->{parameter.type}"
                                    ],
                                },
                            ),
                        )

    def _add_textual_coercion_candidates(
        self,
        skills: List[Fingerprint],
        skill_terms: Dict[str, Set[str]],
        candidates: MutableMapping[Tuple[str, str], RelationCandidate],
    ) -> None:
        for source in skills:
            source_terms = skill_terms[source.id]
            for target in skills:
                if source.id == target.id:
                    continue
                target_terms = skill_terms[target.id]
                shared_terms = sorted(source_terms & target_terms)
                for output in source.outputs:
                    for parameter in target.inputs:
                        if not _can_feed_by_type(output.type, parameter.type):
                            continue
                        input_terms = _tokenize(
                            f"{parameter.name} {parameter.description}"
                        )
                        if not _can_feed_via_textual_coercion(
                            output,
                            parameter,
                            input_terms,
                        ):
                            continue
                        mapping = _port_mapping(
                            output,
                            parameter,
                            match_method="textual_coercion_match",
                            match_reason=_match_reason_textual_coercion(
                                output,
                                parameter,
                            ),
                        )
                        self._merge_candidate(
                            candidates,
                            RelationCandidate(
                                source_id=source.id,
                                target_id=target.id,
                                relation_hints=["can_feed"],
                                candidate_methods=["textual_coercion_match"],
                                priority="low",
                                evidence={
                                    "matched_terms": shared_terms,
                                    "source_outputs": [output.to_dict()],
                                    "target_inputs": [parameter.to_dict()],
                                    "port_mappings": [mapping],
                                    "matched_types": [
                                        f"{output.type}->{parameter.type}"
                                    ],
                                },
                            ),
                        )

    def _merge_candidate(
        self,
        candidates: MutableMapping[Tuple[str, str], RelationCandidate],
        candidate: RelationCandidate,
    ) -> None:
        relation_hints = [
            hint for hint in candidate.relation_hints if hint in ALLOWED_RELATION_TYPES
        ]
        if not relation_hints:
            return
        _debug_candidate_generated(candidate, relation_hints)
        key = (candidate.source_id, candidate.target_id)
        existing = candidates.get(key)
        if existing is None:
            candidates[key] = self.evidence_merger.create(candidate, relation_hints)
            return

        candidates[key] = self.evidence_merger.merge(existing, candidate, relation_hints)

    def _limit_per_skill_relation(
        self, candidates: List[RelationCandidate]
    ) -> List[RelationCandidate]:
        buckets: Dict[str, List[RelationCandidate]] = defaultdict(list)
        for candidate in candidates:
            buckets[candidate.source_id].append(candidate)

        limited: List[RelationCandidate] = []
        for key in sorted(buckets):
            ordered_bucket = sorted(
                buckets[key],
                key=lambda item: (
                    -PRIORITY_RANK.get(item.priority, 0),
                    _candidate_quality_rank(item),
                    item.target_id,
                    ",".join(item.relation_hints),
                ),
            )
            kept = ordered_bucket[: self.max_candidates_per_skill_relation]
            dropped = ordered_bucket[self.max_candidates_per_skill_relation:]
            limited.extend(kept)
            for candidate in dropped:
                LOGGER.debug(
                    "candidate_skipped reason=max_candidates_per_skill_relation "
                    "source=%s target=%s priority=%s methods=%s "
                    "relation_hints=%s limit=%s",
                    candidate.source_id,
                    candidate.target_id,
                    candidate.priority,
                    ",".join(candidate.candidate_methods),
                    ",".join(candidate.relation_hints),
                    self.max_candidates_per_skill_relation,
                )
        return sorted(
            limited,
            key=lambda item: (
                item.source_id,
                -PRIORITY_RANK.get(item.priority, 0),
                item.target_id,
                ",".join(item.relation_hints),
            ),
        )


class CandidateIndexes:
    def __init__(self) -> None:
        self.by_output_name: Dict[str, Set[str]] = defaultdict(set)
        self.by_input_name: Dict[str, Set[str]] = defaultdict(set)
        self.outputs_by_skill_name: Dict[Tuple[str, str], List[ArtifactSpec]] = (
            defaultdict(list)
        )
        self.inputs_by_skill_name: Dict[Tuple[str, str], List[ParameterSpec]] = (
            defaultdict(list)
        )

    @classmethod
    def from_skills(cls, skills: Iterable[Fingerprint]) -> "CandidateIndexes":
        indexes = cls()
        for skill in skills:
            for output in skill.outputs:
                indexes.by_output_name[output.name].add(skill.id)
                indexes.outputs_by_skill_name[(skill.id, output.name)].append(output)
            for parameter in skill.inputs:
                indexes.by_input_name[parameter.name].add(skill.id)
                indexes.inputs_by_skill_name[(skill.id, parameter.name)].append(
                    parameter
                )
        return indexes


class CandidateEvidenceMerger:
    """Merge directional evidence for repeated source-target candidates."""

    def __init__(self, *, max_port_mappings: int) -> None:
        self.max_port_mappings = max(1, max_port_mappings)

    def create(
        self,
        candidate: RelationCandidate,
        relation_hints: List[str],
    ) -> RelationCandidate:
        return self._limit_port_mappings(
            RelationCandidate(
                source_id=candidate.source_id,
                target_id=candidate.target_id,
                relation_hints=sorted(set(relation_hints)),
                candidate_methods=sorted(set(candidate.candidate_methods)),
                priority=candidate.priority,
                evidence=self._directional_evidence(candidate),
            )
        )

    def merge(
        self,
        existing: RelationCandidate,
        candidate: RelationCandidate,
        relation_hints: List[str],
    ) -> RelationCandidate:
        priority = (
            candidate.priority
            if PRIORITY_RANK.get(candidate.priority, 0)
            > PRIORITY_RANK.get(existing.priority, 0)
            else existing.priority
        )
        evidence = _merge_directional_evidence(
            existing.evidence,
            self._directional_evidence(candidate),
        )
        return self._limit_port_mappings(
            RelationCandidate(
                source_id=existing.source_id,
                target_id=existing.target_id,
                relation_hints=sorted(set(existing.relation_hints) | set(relation_hints)),
                candidate_methods=sorted(
                    set(existing.candidate_methods) | set(candidate.candidate_methods)
                ),
                priority=priority,
                evidence=evidence,
            )
        )

    @staticmethod
    def _directional_evidence(candidate: RelationCandidate) -> Dict[str, object]:
        direction_key = f"{candidate.source_id}->{candidate.target_id}"
        return {"directions": {direction_key: candidate.evidence}}

    def _limit_port_mappings(self, candidate: RelationCandidate) -> RelationCandidate:
        return _limit_port_mappings_for_candidate(candidate, self.max_port_mappings)


def _merge_evidence(
    left: Dict[str, object],
    right: Dict[str, object],
) -> Dict[str, object]:
    merged = dict(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = value
            continue
        if isinstance(merged[key], list) and isinstance(value, list):
            merged_value = _dedupe_list(merged[key] + value)
            if key == "matched_types":
                merged[key] = sorted(str(item) for item in merged_value)
            else:
                merged[key] = merged_value
    return merged


def _merge_directional_evidence(
    left: Dict[str, object],
    right: Dict[str, object],
) -> Dict[str, object]:
    merged = dict(left)
    directions = dict(merged.get("directions", {}))
    for direction, evidence in dict(right.get("directions", {})).items():
        if (
            direction in directions
            and isinstance(directions[direction], dict)
            and isinstance(evidence, dict)
        ):
            directions[direction] = _merge_evidence(directions[direction], evidence)
        else:
            directions[direction] = evidence
    merged["directions"] = directions
    return merged


def _limit_port_mappings_for_candidate(
    candidate: RelationCandidate,
    max_port_mappings: int,
) -> RelationCandidate:
    directions = {}
    for direction, evidence in dict(candidate.evidence.get("directions", {})).items():
        if not isinstance(evidence, dict):
            directions[direction] = evidence
            continue
        limited_evidence = dict(evidence)
        mappings = [
            mapping
            for mapping in limited_evidence.get("port_mappings", [])
            if isinstance(mapping, dict)
        ]
        limited_evidence["port_mappings"] = _dedupe_port_mappings(
            sorted(mappings, key=_port_mapping_sort_key)
        )[:max_port_mappings]
        directions[direction] = limited_evidence
    return RelationCandidate(
        source_id=candidate.source_id,
        target_id=candidate.target_id,
        relation_hints=candidate.relation_hints,
        candidate_methods=candidate.candidate_methods,
        priority=candidate.priority,
        evidence={**candidate.evidence, "directions": directions},
    )


def _candidate_quality_rank(candidate: RelationCandidate) -> tuple[int, int, int, int]:
    port_mapping_count = 0
    content_input_count = 0
    for evidence in dict(candidate.evidence.get("directions", {})).values():
        if not isinstance(evidence, dict):
            continue
        mappings = [
            mapping
            for mapping in evidence.get("port_mappings", [])
            if isinstance(mapping, dict)
        ]
        port_mapping_count += len(mappings)
        for mapping in mappings:
            target_input = str(mapping.get("target_input") or "")
            if target_input in GENERIC_CONTENT_INPUT_NAMES:
                content_input_count += 1
        for item in evidence.get("target_inputs", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "") in GENERIC_CONTENT_INPUT_NAMES:
                content_input_count += 1
    return (
        -len(set(candidate.candidate_methods)),
        -int(port_mapping_count > 0),
        -port_mapping_count,
        -content_input_count,
    )


def _dedupe_port_mappings(values: List[dict]) -> List[dict]:
    seen = set()
    result = []
    for value in values:
        marker = (
            value.get("source_output"),
            value.get("source_type"),
            value.get("target_input"),
            value.get("target_type"),
        )
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _port_mapping_sort_key(mapping: dict) -> tuple[int, str, str]:
    method_rank = 0 if mapping.get("match_method") == "exact_io_match" else 1
    return (
        method_rank,
        str(mapping.get("source_output") or ""),
        str(mapping.get("target_input") or ""),
    )


def _dedupe_list(values: List[object]) -> List[object]:
    seen = set()
    result = []
    for value in values:
        marker = repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _debug_candidate_generated(
    candidate: RelationCandidate,
    relation_hints: List[str],
) -> None:
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    evidence = candidate.evidence
    LOGGER.debug(
        "candidate_generated source=%s target=%s priority=%s methods=%s "
        "relation_hints=%s matched_terms=%s matched_types=%s",
        candidate.source_id,
        candidate.target_id,
        candidate.priority,
        ",".join(candidate.candidate_methods),
        ",".join(relation_hints),
        ",".join(str(item) for item in evidence.get("matched_terms", [])),
        ",".join(str(item) for item in evidence.get("matched_types", [])),
    )


_DATA_TYPE_VOCAB = DataTypeVocabulary.default()


def _skill_terms(skill: Fingerprint) -> Set[str]:
    terms: Set[str] = set()
    chunks = [skill.id, skill.name, skill.description]
    chunks.extend(item.name for item in skill.inputs)
    chunks.extend(item.description for item in skill.inputs)
    chunks.extend(item.name for item in skill.outputs)
    chunks.extend(item.description for item in skill.outputs)
    for chunk in chunks:
        terms.update(_tokenize(chunk))
    return terms


def _has_semantic_overlap(
    output: ArtifactSpec,
    parameter: ParameterSpec,
) -> bool:
    if output.name == parameter.name:
        return True
    output_terms = _tokenize(f"{output.name} {output.description}")
    input_terms = _tokenize(f"{parameter.name} {parameter.description}")
    return bool(output_terms & input_terms)


def _can_feed_by_type(output_type: str, input_type: str) -> bool:
    return _DATA_TYPE_VOCAB.can_feed_by_type(output_type, input_type)


def _can_feed_by_remote_reference(output: ArtifactSpec, parameter: ParameterSpec) -> bool:
    if output.type != "url":
        return False
    if parameter.type not in {"file", "image", "text"}:
        return False
    return _accepts_remote_reference(parameter)


def _accepts_remote_reference(parameter: ParameterSpec) -> bool:
    inference = _DATA_TYPE_VOCAB.infer_from_io_semantics(
        parameter.name,
        parameter.description,
    )
    return inference is not None and inference.data_type == "url"


def _can_feed_by_exact_or_subtype(output_type: str, input_type: str) -> bool:
    if output_type == input_type:
        return True
    return _DATA_TYPE_VOCAB.is_subtype(output_type, input_type)


def _can_feed_via_textual_coercion(
    output: ArtifactSpec,
    parameter: ParameterSpec,
    input_terms: Set[str],
) -> bool:
    if output.type not in _DATA_TYPE_VOCAB.CONTENT_CARRIER_TYPES:
        return False
    if parameter.type not in _DATA_TYPE_VOCAB.CONTENT_CARRIER_TYPES:
        return False
    if not _is_generic_content_input(parameter, input_terms):
        return False
    return True


def _is_generic_content_input(parameter: ParameterSpec, input_terms: Set[str]) -> bool:
    return parameter.name in GENERIC_CONTENT_INPUT_NAMES or bool(
        input_terms & GENERIC_CONTENT_INPUT_NAMES
    )


def _port_mapping(
    output: ArtifactSpec,
    parameter: ParameterSpec,
    *,
    match_method: str,
    match_reason: str,
) -> dict:
    return {
        "source_output": output.name,
        "source_type": output.type,
        "target_input": parameter.name,
        "target_type": parameter.type,
        "match_reason": match_reason,
        "match_method": match_method,
    }


def _match_reason_exact_io(output_type: str, input_type: str) -> str:
    if output_type == input_type:
        return "source output and target input share the same normalized name and type"
    return (
        f"source output type '{output_type}' is a subtype of "
        f"target input type '{input_type}'"
    )


def _match_reason_semantic_overlap(
    output: ArtifactSpec,
    parameter: ParameterSpec,
) -> str:
    if output.name == parameter.name:
        return "source output and target input share the same normalized name"
    return "source output and target input share semantic terms"


def _match_reason_remote_reference(
    output: ArtifactSpec,
    parameter: ParameterSpec,
) -> str:
    del output, parameter
    return "source output URL can satisfy target file-like input that accepts remote references"


def _match_reason_textual_coercion(
    output: ArtifactSpec,
    parameter: ParameterSpec,
) -> str:
    del output, parameter
    return "textual output can be coerced to generic content input"


def _tokenize(text: str) -> Set[str]:
    return _GRAPH_TERM_LEXICON.tokenize(text)
