"""Data contracts for offline Skill graph construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from jiuwenswarm.symphony.fingerprint.models import Fingerprint


ALLOWED_RELATION_TYPES = frozenset(
    {
        "can_feed",
    }
)


@dataclass(frozen=True)
class GraphDiagnostic:
    """Structured diagnostic emitted during graph construction."""

    stage: str
    severity: str
    code: str
    message: str
    skill_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "stage": self.stage,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class SkillRegistry:
    """Validated Skill registry keyed by Skill ID."""

    skills: Dict[str, Fingerprint]
    diagnostics: List[GraphDiagnostic] = field(default_factory=list)

    def ordered_skills(self) -> List[Fingerprint]:
        return [self.skills[skill_id] for skill_id in sorted(self.skills)]


@dataclass(frozen=True)
class RelationCandidate:
    """A cheap, deterministic Skill pair candidate for LLM review."""

    source_id: str
    target_id: str
    relation_hints: List[str]
    candidate_methods: List[str]
    priority: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source_id}->{self.target_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.key,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation_hints": list(self.relation_hints),
            "candidate_methods": list(self.candidate_methods),
            "priority": self.priority,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class LLMMatch:
    """LLM relation judgment, after schema-level normalization."""

    source_id: str
    target_id: str
    relation_type: str
    confidence: float
    method: str = "llm_ontology_match"
    reasons: List[str] = field(default_factory=list)
    supporting_fields: Dict[str, Any] = field(default_factory=dict)
    candidate_id: Optional[str] = None
    accepted: bool = False
    diagnostics: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation_type": self.relation_type,
            "confidence": self.confidence,
            "method": self.method,
            "reasons": list(self.reasons),
            "supporting_fields": self.supporting_fields,
            "accepted": self.accepted,
            "diagnostics": list(self.diagnostics),
            "raw": self.raw,
        }


@dataclass(frozen=True)
class GraphNode:
    """A typed graph node."""

    id: str
    type: str
    label: str
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "properties": self.properties,
        }


@dataclass(frozen=True)
class GraphEdge:
    """A typed graph edge."""

    source: str
    target: str
    type: str
    confidence: float = 1.0
    method: str = "deterministic"
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}->{self.target}:{self.type}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "confidence": self.confidence,
            "method": self.method,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SkillGraph:
    """Skill graph nodes and typed edges."""

    nodes: List[GraphNode]
    edges: List[GraphEdge]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True)
class ScoreLookup:
    """Lookup tables used by online Score retrieval and planning."""

    by_output: Dict[str, List[str]]
    by_input: Dict[str, List[str]]
    by_data_type: Dict[str, List[str]]
    neighbors: Dict[str, List[str]]
    upstream_by_input: Dict[str, List[str]]
    downstream_by_output: Dict[str, List[str]]
    by_text_term: Dict[str, List[str]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "by_output": self.by_output,
            "by_input": self.by_input,
            "by_data_type": self.by_data_type,
            "neighbors": self.neighbors,
            "upstream_by_input": self.upstream_by_input,
            "downstream_by_output": self.downstream_by_output,
            "by_text_term": self.by_text_term,
        }


@dataclass(frozen=True)
class BuildManifest:
    """Manifest used as the online loading entrypoint."""

    schema_version: str = "Orchestration-score-v1"
    artifacts: Dict[str, str] = field(
        default_factory=lambda: {
            "skills": "skills.json",
            "graph": "skill_graph.json",
            "score_lookup": "score_lookup.json",
            "llm_matches": "llm_matches.json",
            "diagnostics": "diagnostics.json",
            "io_name_vocab": "io_name_vocab.json",
        }
    )
    thresholds: Dict[str, float] = field(
        default_factory=lambda: {
            "can_feed": 0.7,
        }
    )
    planning_defaults: Dict[str, Any] = field(
        default_factory=lambda: {
            "min_edge_confidence": 0.7,
            "max_depth": 4,
            "max_plans": 20,
            "max_branch": 8,
            "max_entry_skills": 40,
            "top_m": 12,
            "top_k": 3,
            "include_candidates": True,
            "conservative_reject": True,
            "enable_backward_search": True,
        }
    )
    candidate_generation: Dict[str, Any] = field(default_factory=dict)
    llm: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifacts": self.artifacts,
            "thresholds": self.thresholds,
            "planning_defaults": self.planning_defaults,
            "candidate_generation": self.candidate_generation,
            "llm": self.llm,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class GraphBuildResult:
    """Complete graph build result."""

    manifest: BuildManifest
    skills: List[Fingerprint]
    candidates: List[RelationCandidate]
    llm_matches: List[LLMMatch]
    graph: SkillGraph
    lookup: ScoreLookup
    diagnostics: List[GraphDiagnostic] = field(default_factory=list)
