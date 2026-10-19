"""Offline graph construction orchestration."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from jiuwenswarm.symphony.llm import get_llm_token_usage_summary
from jiuwenswarm.symphony.graph.builders import ScoreLookupBuilder, SkillGraphBuilder
from jiuwenswarm.symphony.graph.candidates import CandidateGenerator
from jiuwenswarm.symphony.graph.matcher import (
    DEFAULT_THRESHOLDS,
    OntologyMatcher,
)
from jiuwenswarm.symphony.graph.models import BuildManifest, GraphBuildResult, GraphDiagnostic
from jiuwenswarm.symphony.graph.registry import SkillRegistryBuilder
from jiuwenswarm.symphony.fingerprint.models import Fingerprint

GraphProgress = Callable[..., None]


class GraphBuilder:
    """Build Skill graph artifacts from normalized Skill fingerprints."""

    def __init__(
        self,
        *,
        matcher: OntologyMatcher,
        registry_builder: Optional[SkillRegistryBuilder] = None,
        candidate_generator: Optional[CandidateGenerator] = None,
        graph_builder: Optional[SkillGraphBuilder] = None,
        lookup_builder: Optional[ScoreLookupBuilder] = None,
    ) -> None:
        self.matcher = matcher
        self.registry_builder = registry_builder or SkillRegistryBuilder()
        self.candidate_generator = candidate_generator or CandidateGenerator()
        self.graph_builder = graph_builder or SkillGraphBuilder()
        self.lookup_builder = lookup_builder or ScoreLookupBuilder()

    async def __call__(
        self,
        fingerprints: Iterable[Fingerprint],
        *,
        progress: GraphProgress | None = None,
    ) -> GraphBuildResult:
        return await self.build(fingerprints, progress=progress)

    async def build(
        self,
        fingerprints: Iterable[Fingerprint],
        *,
        progress: GraphProgress | None = None,
    ) -> GraphBuildResult:
        fingerprints = list(fingerprints)
        diagnostics: list[GraphDiagnostic] = []
        _emit_progress(progress, "graph.registry.start", fingerprint_count=len(fingerprints))
        registry = self.registry_builder.register(fingerprints)
        diagnostics.extend(registry.diagnostics)
        _emit_progress(
            progress,
            "graph.registry.done",
            skill_count=len(registry.skills),
            diagnostics_count=len(registry.diagnostics),
        )

        _emit_progress(progress, "graph.candidates.start", skill_count=len(registry.skills))
        candidates = self.candidate_generator.generate(registry)
        _emit_progress(progress, "graph.candidates.done", candidate_count=len(candidates))

        _emit_progress(progress, "graph.resolve.start", candidate_count=len(candidates))
        llm_matches = await self.matcher.match(registry, candidates)
        relation_diagnostics = self._relation_diagnostics(llm_matches)
        diagnostics.extend(relation_diagnostics)
        _emit_progress(
            progress,
            "graph.resolve.done",
            candidate_count=len(candidates),
            match_count=len(llm_matches),
            accepted_match_count=sum(1 for match in llm_matches if match.accepted),
            diagnostics_count=len(relation_diagnostics),
        )

        _emit_progress(progress, "graph.materialize.start", match_count=len(llm_matches))
        graph = self.graph_builder.build(registry, llm_matches)
        _emit_progress(
            progress,
            "graph.materialize.done",
            node_count=len(graph.nodes),
            edge_count=len(graph.edges),
        )

        _emit_progress(
            progress,
            "graph.score.start",
            node_count=len(graph.nodes),
            edge_count=len(graph.edges),
        )
        lookup = self.lookup_builder.build(registry, graph)
        _emit_progress(progress, "graph.score.done")

        llm_metadata = _matcher_metadata(self.matcher)
        llm_metadata["token_usage"] = get_llm_token_usage_summary()
        manifest = BuildManifest(
            thresholds=_matcher_thresholds(self.matcher),
            candidate_generation=_candidate_generator_metadata(
                self.candidate_generator
            ),
            llm=llm_metadata,
        )

        return GraphBuildResult(
            manifest=manifest,
            skills=registry.ordered_skills(),
            candidates=candidates,
            llm_matches=llm_matches,
            graph=graph,
            lookup=lookup,
            diagnostics=diagnostics,
        )

    def _relation_diagnostics(self, llm_matches: list) -> list[GraphDiagnostic]:
        diagnostics: list[GraphDiagnostic] = []
        matcher_diagnostics = []
        if hasattr(self.matcher, "diagnostics"):
            matcher_diagnostics = list(self.matcher.diagnostics)
            diagnostics.extend(matcher_diagnostics)

        for match in llm_matches:
            if matcher_diagnostics:
                continue
            for message in match.diagnostics:
                diagnostics.append(
                    GraphDiagnostic(
                        stage="llm_match",
                        severity="warning",
                        code="match_diagnostic",
                        message=message,
                        skill_id=match.source_id,
                        details={"match": match.to_dict()},
                    )
                )
        return diagnostics


def _emit_progress(progress: GraphProgress | None, stage: str, **details: Any) -> None:
    if progress is not None:
        progress(stage, **details)


def _matcher_metadata(matcher: OntologyMatcher) -> dict:
    if hasattr(matcher, "manifest_metadata"):
        return matcher.manifest_metadata()
    return {"matcher": matcher.__class__.__name__}


def _matcher_thresholds(matcher: OntologyMatcher) -> dict:
    if hasattr(matcher, "thresholds"):
        return dict(matcher.thresholds)
    return dict(DEFAULT_THRESHOLDS)


def _candidate_generator_metadata(candidate_generator: CandidateGenerator) -> dict:
    return {
        "max_candidates_per_skill_relation": int(
            candidate_generator.max_candidates_per_skill_relation
        ),
        "max_port_mappings_per_candidate": int(
            candidate_generator.max_port_mappings_per_candidate
        ),
        "max_exact_io_pair_fanout": int(
            candidate_generator.max_exact_io_pair_fanout
        ),
    }
