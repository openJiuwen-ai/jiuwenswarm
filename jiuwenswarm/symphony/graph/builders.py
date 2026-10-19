"""Build typed Skill graph and Score lookup artifacts."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Set

from jiuwenswarm.symphony.graph.lexicon import (
    ArtifactLexicon,
    DEFAULT_GRAPH_STOP_TERMS,
)
from jiuwenswarm.symphony.graph.models import (
    GraphEdge,
    GraphNode,
    LLMMatch,
    ScoreLookup,
    SkillGraph,
    SkillRegistry,
)


IO_NAMES_EXCLUDED_FROM_EXACT_LOOKUP = frozenset(
    {
        "dependencies",
        "path",
    }
)
_GRAPH_TERM_LEXICON = ArtifactLexicon.create(
    stop_terms=DEFAULT_GRAPH_STOP_TERMS,
    min_token_length=3,
)


class SkillGraphBuilder:
    """Build Skill graph nodes and accepted can_feed edges."""

    @staticmethod
    def build(
        registry: SkillRegistry,
        llm_matches: Iterable[LLMMatch],
    ) -> SkillGraph:
        nodes: Dict[str, GraphNode] = {}
        edges: Dict[str, GraphEdge] = {}

        for skill in registry.ordered_skills():
            nodes.setdefault(
                f"skill:{skill.id}",
                GraphNode(
                    id=f"skill:{skill.id}",
                    type="skill",
                    label=skill.name or skill.id,
                    properties={
                        "skill_id": skill.id,
                        "name": skill.name,
                        "description": skill.description,
                        "version": skill.version,
                        "inputs": [item.to_dict() for item in skill.inputs],
                        "outputs": [item.to_dict() for item in skill.outputs],
                    },
                ),
            )

        for match in llm_matches:
            if not match.accepted or match.relation_type != "can_feed":
                continue
            edge = GraphEdge(
                source=f"skill:{match.source_id}",
                target=f"skill:{match.target_id}",
                type=match.relation_type,
                confidence=match.confidence,
                method=match.method,
                evidence={
                    "candidate_id": match.candidate_id,
                    "reasons": match.reasons,
                    "supporting_fields": match.supporting_fields,
                },
            )
            edges.setdefault(edge.key, edge)

        ordered_nodes = []
        for node_id in sorted(nodes):
            node = nodes.get(node_id)
            if node is not None:
                ordered_nodes.append(node)
        return SkillGraph(
            nodes=ordered_nodes,
            edges=sorted(edges.values(), key=lambda edge: edge.key),
        )


class ScoreLookupBuilder:
    """Build deterministic lookup tables for online Score retrieval."""

    def __init__(
        self,
        *,
        generic_io_names: Iterable[str] = IO_NAMES_EXCLUDED_FROM_EXACT_LOOKUP,
        max_io_bucket_size: int = 16,
        max_text_bucket_size: int = 24,
    ) -> None:
        self.lexicon = ArtifactLexicon.create(
            stop_terms=DEFAULT_GRAPH_STOP_TERMS,
            min_token_length=3,
            generic_io_names=generic_io_names,
        )
        self.max_io_bucket_size = max(1, max_io_bucket_size)
        self.max_text_bucket_size = max(2, max_text_bucket_size)

    def build(self, registry: SkillRegistry, graph: SkillGraph) -> ScoreLookup:
        by_output: Dict[str, Set[str]] = defaultdict(set)
        by_input: Dict[str, Set[str]] = defaultdict(set)
        by_data_type: Dict[str, Set[str]] = defaultdict(set)
        by_text_term: Dict[str, Set[str]] = defaultdict(set)

        for skill in registry.ordered_skills():
            for output in skill.outputs:
                if not self.lexicon.is_generic_io_name(output.name):
                    by_output[output.name].add(skill.id)
                by_data_type[output.type].add(skill.id)
            for parameter in skill.inputs:
                if not self.lexicon.is_generic_io_name(parameter.name):
                    by_input[parameter.name].add(skill.id)
                by_data_type[parameter.type].add(skill.id)
            for term in _skill_terms(skill):
                by_text_term[term].add(skill.id)

        neighbors: Dict[str, Set[str]] = defaultdict(set)
        upstream_by_input: Dict[str, Set[str]] = defaultdict(set)
        downstream_by_output: Dict[str, Set[str]] = defaultdict(set)
        skill_inputs = {
            skill.id: {parameter.name for parameter in skill.inputs}
            for skill in registry.ordered_skills()
        }
        skill_outputs = {
            skill.id: {output.name for output in skill.outputs}
            for skill in registry.ordered_skills()
        }

        for edge in graph.edges:
            if edge.source.startswith("skill:") and edge.target.startswith("skill:"):
                source_id = edge.source.removeprefix("skill:")
                target_id = edge.target.removeprefix("skill:")
                neighbors[source_id].add(target_id)
                if edge.type != "can_feed":
                    continue
                shared = sorted(
                    skill_outputs.get(source_id, set())
                    & skill_inputs.get(target_id, set())
                )
                for name in shared:
                    if self.lexicon.is_generic_io_name(name):
                        continue
                    upstream_by_input[name].add(source_id)
                    downstream_by_output[name].add(target_id)

        return ScoreLookup(
            by_output=_freeze_lookup(
                by_output,
                max_bucket_size=self.max_io_bucket_size,
            ),
            by_input=_freeze_lookup(
                by_input,
                max_bucket_size=self.max_io_bucket_size,
            ),
            by_data_type=_freeze_lookup(by_data_type),
            neighbors=_freeze_lookup(neighbors),
            upstream_by_input=_freeze_lookup(
                upstream_by_input,
                max_bucket_size=self.max_io_bucket_size,
            ),
            downstream_by_output=_freeze_lookup(
                downstream_by_output,
                max_bucket_size=self.max_io_bucket_size,
            ),
            by_text_term=_freeze_lookup(
                by_text_term,
                max_bucket_size=self.max_text_bucket_size,
            ),
        )


def _freeze_lookup(
    lookup: Dict[str, Set[str]],
    *,
    max_bucket_size: int | None = None,
) -> Dict[str, List[str]]:
    frozen = {}
    for key, values in sorted(lookup.items()):
        if max_bucket_size is not None and len(values) > max_bucket_size:
            continue
        frozen[key] = sorted(values)
    return frozen


def _skill_terms(skill) -> Set[str]:
    chunks: List[str] = [skill.id, skill.name, skill.description]
    chunks.extend(item.name for item in skill.inputs)
    chunks.extend(item.description for item in skill.inputs)
    chunks.extend(item.name for item in skill.outputs)
    chunks.extend(item.description for item in skill.outputs)
    terms: Set[str] = set()
    for chunk in chunks:
        terms.update(_GRAPH_TERM_LEXICON.tokenize(chunk))
    return terms
