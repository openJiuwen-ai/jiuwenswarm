import pytest

from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ParameterSpec,
    Fingerprint,
)
from jiuwenswarm.symphony.graph.builders import ScoreLookupBuilder
from jiuwenswarm.symphony.graph.candidates import CandidateGenerator
from jiuwenswarm.symphony.graph.pipeline import GraphBuilder
from jiuwenswarm.symphony.graph.models import (
    GraphEdge,
    LLMMatch,
    SkillGraph,
    SkillRegistry,
)


def test_candidate_generator_emits_exact_io_match():
    candidates = _candidates(
        _skill("source", outputs=[ArtifactSpec(name="summary", type="text")]),
        _skill("target", inputs=[ParameterSpec(name="summary", type="text")]),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert "exact_io_match" in candidate.candidate_methods
    evidence = candidate.evidence["directions"]["source->target"]
    assert evidence["port_mappings"] == [
        {
            "source_output": "summary",
            "source_type": "text",
            "target_input": "summary",
            "target_type": "text",
            "match_reason": (
                "source output and target input share the same normalized name and type"
            ),
            "match_method": "exact_io_match",
        }
    ]


def test_candidate_generator_default_candidate_limit_is_thirty_two():
    assert CandidateGenerator().max_candidates_per_skill_relation == 32


def test_candidate_generator_keeps_stronger_candidates_before_target_name():
    candidates = CandidateGenerator(max_candidates_per_skill_relation=1).generate(
        SkillRegistry(
            skills={
                "source": _skill(
                    "source",
                    outputs=[
                        ArtifactSpec(
                            name="outline",
                            type="markdown",
                            description="shared research slides evidence",
                        )
                    ],
                ),
                "aaa-weak": _skill(
                    "aaa-weak",
                    inputs=[
                        ParameterSpec(
                            name="report",
                            type="text",
                            description="shared research slides evidence",
                        )
                    ],
                ),
                "zzz-content": _skill(
                    "zzz-content",
                    inputs=[
                        ParameterSpec(
                            name="content",
                            type="markdown",
                            description="shared research slides evidence",
                        )
                    ],
                ),
            }
        )
    )

    assert [candidate.key for candidate in candidates] == ["source->zzz-content"]
    assert candidates[0].candidate_methods == [
        "semantic_overlap_match",
        "textual_coercion_match",
    ]


def test_candidate_generator_keeps_more_port_mapping_evidence_before_target_name():
    candidates = CandidateGenerator(max_candidates_per_skill_relation=1).generate(
        SkillRegistry(
            skills={
                "source": _skill(
                    "source",
                    outputs=[
                        ArtifactSpec(
                            name="brief",
                            type="text",
                            description="shared planning evidence",
                        ),
                        ArtifactSpec(
                            name="outline",
                            type="text",
                            description="shared planning evidence",
                        ),
                    ],
                ),
                "aaa-weak": _skill(
                    "aaa-weak",
                    inputs=[
                        ParameterSpec(
                            name="request",
                            type="text",
                            description="shared planning evidence",
                        )
                    ],
                ),
                "zzz-rich": _skill(
                    "zzz-rich",
                    inputs=[
                        ParameterSpec(
                            name="content",
                            type="text",
                            description="shared planning evidence",
                        ),
                        ParameterSpec(
                            name="text",
                            type="text",
                            description="shared planning evidence",
                        ),
                    ],
                ),
            }
        )
    )

    assert [candidate.key for candidate in candidates] == ["source->zzz-rich"]
    evidence = candidates[0].evidence["directions"]["source->zzz-rich"]
    assert len(evidence["port_mappings"]) > 1


def test_candidate_generator_candidate_limit_sorting_is_stable():
    skills = [
        _skill(
            "source",
            outputs=[
                ArtifactSpec(
                    name="outline",
                    type="markdown",
                    description="shared research slides evidence",
                )
            ],
        ),
        _skill(
            "aaa-weak",
            inputs=[
                ParameterSpec(
                    name="report",
                    type="text",
                    description="shared research slides evidence",
                )
            ],
        ),
        _skill(
            "zzz-content",
            inputs=[
                ParameterSpec(
                    name="content",
                    type="markdown",
                    description="shared research slides evidence",
                )
            ],
        ),
    ]

    first = CandidateGenerator(max_candidates_per_skill_relation=1).generate(
        SkillRegistry(skills={skill.id: skill for skill in skills})
    )
    second = CandidateGenerator(max_candidates_per_skill_relation=1).generate(
        SkillRegistry(skills={skill.id: skill for skill in reversed(skills)})
    )

    assert [candidate.key for candidate in first] == [
        candidate.key for candidate in second
    ]


def test_candidate_generator_keeps_reverse_direction_candidates_distinct():
    candidates = _candidates(
        _skill(
            "alpha",
            inputs=[ParameterSpec(name="beta_payload", type="text")],
            outputs=[ArtifactSpec(name="alpha_payload", type="text")],
        ),
        _skill(
            "beta",
            inputs=[ParameterSpec(name="alpha_payload", type="text")],
            outputs=[ArtifactSpec(name="beta_payload", type="text")],
        ),
    )

    assert [(item.source_id, item.target_id) for item in candidates] == [
        ("alpha", "beta"),
        ("beta", "alpha"),
    ]
    assert [item.key for item in candidates] == [
        "alpha->beta",
        "beta->alpha",
    ]
    assert "alpha->beta" in candidates[0].evidence["directions"]
    assert "beta->alpha" in candidates[1].evidence["directions"]


def test_candidate_generator_suppresses_ambiguous_exact_io_candidate_name():
    candidates = _candidates(
        _skill("source", outputs=[ArtifactSpec(name="dependencies", type="text")]),
        _skill("target", inputs=[ParameterSpec(name="dependencies", type="text")]),
    )

    assert candidates == []


def test_candidate_generator_treats_code_as_regular_exact_io_name():
    candidates = _candidates(
        _skill("source", outputs=[ArtifactSpec(name="code", type="text")]),
        _skill("target", inputs=[ParameterSpec(name="code", type="text")]),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert "exact_io_match" in candidate.candidate_methods


def test_candidate_generator_emits_semantic_overlap_match():
    candidates = _candidates(
        _skill(
            "source",
            outputs=[
                ArtifactSpec(
                    name="summary",
                    type="text",
                    description="document findings and action items",
                )
            ],
        ),
        _skill(
            "target",
            inputs=[
                ParameterSpec(
                    name="report",
                    type="text",
                    description="document findings and action items",
                )
            ],
        ),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert "semantic_overlap_match" in candidate.candidate_methods
    evidence = candidate.evidence["directions"]["source->target"]
    assert evidence["port_mappings"] == [
        {
            "source_output": "summary",
            "source_type": "text",
            "target_input": "report",
            "target_type": "text",
            "match_reason": "source output and target input share semantic terms",
            "match_method": "semantic_overlap_match",
        }
    ]


def test_candidate_generator_merges_multiple_matched_types_without_stale_scalar():
    candidates = _candidates(
        _skill(
            "source",
            outputs=[
                ArtifactSpec(
                    name="summary",
                    type="text",
                    description="shared topic evidence",
                ),
                ArtifactSpec(
                    name="asset_url",
                    type="url",
                    description="remote URL",
                ),
            ],
        ),
        _skill(
            "target",
            inputs=[
                ParameterSpec(
                    name="brief",
                    type="text",
                    description="shared topic evidence",
                ),
                ParameterSpec(
                    name="asset_file",
                    type="file",
                    description="accepts HTTP or HTTPS URL",
                ),
            ],
        ),
    )

    assert len(candidates) == 1
    evidence = candidates[0].evidence["directions"]["source->target"]
    assert evidence["matched_types"] == [
        "text->text",
        "url->file",
    ]
    assert "matched_type" not in evidence


def test_candidate_generator_emits_textual_coercion_match():
    candidates = _candidates(
        _skill("source", outputs=[ArtifactSpec(name="article", type="text")]),
        _skill("target", inputs=[ParameterSpec(name="content", type="text")]),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert candidate.priority == "low"
    assert candidate.candidate_methods == ["textual_coercion_match"]
    evidence = candidate.evidence["directions"]["source->target"]
    assert evidence["port_mappings"] == [
        {
            "source_output": "article",
            "source_type": "text",
            "target_input": "content",
            "target_type": "text",
            "match_reason": "textual output can be coerced to generic content input",
            "match_method": "textual_coercion_match",
        }
    ]


def test_candidate_generator_skips_textual_coercion_for_control_destination_inputs():
    for input_name in ("to", "cc", "bcc", "limit", "command"):
        assert (
            _candidates(
                _skill("source", outputs=[ArtifactSpec(name="article", type="text")]),
                _skill("target", inputs=[ParameterSpec(name=input_name, type="text")]),
            )
            == []
        )


def test_candidate_generator_emits_remote_reference_candidate_for_url_file_like_input():
    candidates = _candidates(
        _skill(
            "source",
            outputs=[
                ArtifactSpec(
                    name="asset_reference",
                    type="url",
                    description="remote URL",
                )
            ],
        ),
        _skill(
            "target",
            inputs=[
                ParameterSpec(
                    name="file",
                    type="file",
                    description="accepts HTTP or HTTPS URL",
                )
            ],
        ),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert "semantic_overlap_match" in candidate.candidate_methods
    evidence = candidate.evidence["directions"]["source->target"]
    assert evidence["port_mappings"] == [
        {
            "source_output": "asset_reference",
            "source_type": "url",
            "target_input": "file",
            "target_type": "file",
            "match_reason": (
                "source output URL can satisfy target file-like input that accepts "
                "remote references"
            ),
            "match_method": "semantic_overlap_match",
        }
    ]


def test_candidate_generator_emits_remote_reference_candidate_for_url_text_input():
    candidates = _candidates(
        _skill(
            "source",
            outputs=[
                ArtifactSpec(
                    name="translated_image_url",
                    type="url",
                    description="URL of the translated image.",
                )
            ],
        ),
        _skill(
            "target",
            inputs=[
                ParameterSpec(
                    name="image_url",
                    type="text",
                    description="Public image URL to process.",
                )
            ],
        ),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert "semantic_overlap_match" in candidate.candidate_methods
    evidence = candidate.evidence["directions"]["source->target"]
    assert evidence["port_mappings"][0]["source_output"] == "translated_image_url"
    assert evidence["port_mappings"][0]["target_input"] == "image_url"


def test_candidate_generator_emits_image_artifact_candidate():
    candidates = _candidates(
        _skill(
            "source",
            outputs=[
                ArtifactSpec(
                    name="translated_image_url",
                    type="image",
                    description="URL of the translated image.",
                )
            ],
        ),
        _skill(
            "target",
            inputs=[
                ParameterSpec(
                    name="image",
                    type="image",
                    description="Image input to process.",
                )
            ],
        ),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_id == "source"
    assert candidate.target_id == "target"
    assert "semantic_overlap_match" in candidate.candidate_methods
    evidence = candidate.evidence["directions"]["source->target"]
    assert evidence["port_mappings"][0]["source_output"] == "translated_image_url"
    assert evidence["port_mappings"][0]["target_input"] == "image"


def test_candidate_generator_emits_remote_reference_for_explicit_url_descriptions():
    for description in ("accepts remote URL", "download URL"):
        candidates = _candidates(
            _skill(
                "source",
                outputs=[
                    ArtifactSpec(
                        name="asset_reference",
                        type="url",
                        description="remote URL",
                    )
                ],
            ),
            _skill(
                "target",
                inputs=[
                    ParameterSpec(
                        name="file",
                        type="file",
                        description=description,
                    )
                ],
            ),
        )

        assert len(candidates) == 1
        evidence = candidates[0].evidence["directions"]["source->target"]
        assert evidence["port_mappings"][0]["source_type"] == "url"
        assert evidence["port_mappings"][0]["target_type"] == "file"


def test_candidate_generator_skips_remote_reference_for_broad_web_descriptions():
    for description in ("web asset", "website content"):
        assert (
            _candidates(
                _skill(
                    "source",
                    outputs=[
                        ArtifactSpec(
                            name="asset_reference",
                            type="url",
                            description="remote URL",
                        )
                    ],
                ),
                _skill(
                    "target",
                    inputs=[
                        ParameterSpec(
                            name="file",
                            type="file",
                            description=description,
                        )
                    ],
                ),
            )
            == []
        )


@pytest.mark.asyncio
async def test_graph_builder_does_not_materialize_rejected_exact_io_match():
    result = await GraphBuilder(matcher=_RejectedExactMatcher()).build(
        [
            _skill("source", outputs=[ArtifactSpec(name="summary", type="text")]),
            _skill("target", inputs=[ParameterSpec(name="summary", type="text")]),
        ]
    )

    assert result.candidates
    assert "exact_io_match" in result.candidates[0].candidate_methods
    assert result.llm_matches == [
        LLMMatch(
            source_id="source",
            target_id="target",
            relation_type="can_feed",
            confidence=0.2,
            accepted=False,
            candidate_id=result.candidates[0].key,
        )
    ]
    assert result.graph.edges == []


@pytest.mark.asyncio
async def test_graph_builder_materializes_accepted_exact_io_match():
    result = await GraphBuilder(matcher=_AcceptedExactMatcher()).build(
        [
            _skill("source", outputs=[ArtifactSpec(name="summary", type="text")]),
            _skill("target", inputs=[ParameterSpec(name="summary", type="text")]),
        ]
    )

    assert len(result.graph.edges) == 1
    edge = result.graph.edges[0]
    assert edge.source == "skill:source"
    assert edge.target == "skill:target"
    assert edge.type == "can_feed"
    assert edge.confidence == 0.9


def test_score_lookup_emits_text_term_lookup():
    registry = SkillRegistry(
        skills={
            "source": _skill(
                "source",
                outputs=[
                    ArtifactSpec(name="code", type="text"),
                    ArtifactSpec(name="summary", type="text"),
                    ArtifactSpec(name="dependencies", type="text"),
                ],
            ),
            "target": _skill(
                "target",
                inputs=[
                    ParameterSpec(name="code", type="text"),
                    ParameterSpec(name="summary", type="text"),
                    ParameterSpec(name="dependencies", type="text"),
                ],
            ),
        }
    )
    graph = SkillGraph(
        nodes=[],
        edges=[
            GraphEdge(
                source="skill:source",
                target="skill:target",
                type="can_feed",
            )
        ],
    )

    lookup = ScoreLookupBuilder().build(registry, graph).to_dict()

    assert "by_text_term" in lookup
    assert lookup["by_output"] == {
        "code": ["source"],
        "summary": ["source"],
    }
    assert lookup["by_input"] == {
        "code": ["target"],
        "summary": ["target"],
    }
    assert lookup["by_text_term"]["code"] == ["source", "target"]
    assert lookup["by_text_term"]["dependencies"] == ["source", "target"]
    assert lookup["by_text_term"]["summary"] == ["source", "target"]
    assert lookup["upstream_by_input"] == {
        "code": ["source"],
        "summary": ["source"],
    }
    assert lookup["downstream_by_output"] == {
        "code": ["target"],
        "summary": ["target"],
    }


def _candidates(*skills: Fingerprint):
    return CandidateGenerator().generate(
        SkillRegistry(skills={skill.id: skill for skill in skills})
    )


def _skill(
    skill_id: str,
    *,
    inputs: list[ParameterSpec] | None = None,
    outputs: list[ArtifactSpec] | None = None,
) -> Fingerprint:
    return Fingerprint(
        type="skill",
        id=skill_id,
        name=skill_id,
        description="",
        version="1.0.0",
        inputs=inputs or [],
        outputs=outputs or [],
    )


class _RejectedExactMatcher:
    thresholds = {"can_feed": 0.7}

    async def match(self, registry, candidates):
        del registry
        assert len(candidates) == 1
        return [
            LLMMatch(
                source_id=candidates[0].source_id,
                target_id=candidates[0].target_id,
                relation_type="can_feed",
                confidence=0.2,
                accepted=False,
                candidate_id=candidates[0].key,
            )
        ]

    @staticmethod
    def manifest_metadata():
        return {"matcher": "rejected-exact"}


class _AcceptedExactMatcher:
    thresholds = {"can_feed": 0.7}

    async def match(self, registry, candidates):
        del registry
        assert len(candidates) == 1
        return [
            LLMMatch(
                source_id=candidates[0].source_id,
                target_id=candidates[0].target_id,
                relation_type="can_feed",
                confidence=0.9,
                accepted=True,
                candidate_id=candidates[0].key,
            )
        ]

    @staticmethod
    def manifest_metadata():
        return {"matcher": "accepted-exact"}
