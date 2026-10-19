import json
from pathlib import Path

import pytest

from jiuwenswarm.symphony.fingerprint.extract.extractor import (
    _SCHEMA_EXTRACTION_PROMPT,
    _build_llm_context,
    schema_from_llm_payload,
)
from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    Fingerprint,
    ParameterSpec,
    RawSkillManifest,
    SkillFolder,
)
from jiuwenswarm.symphony.fingerprint.normalize import (
    IONameCandidate,
    IONameVocabTerm,
    IONameVocabulary,
    LLMIONameResolver,
)
from jiuwenswarm.symphony.fingerprint.normalize.io_name_resolver import (
    _resolution_from_payload,
)
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony import build as build_module
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.graph.matcher.openai import (
    OpenAICompatibleOntologyMatcher,
)
from jiuwenswarm.symphony.graph.matcher import prompt as graph_prompt
from jiuwenswarm.symphony.graph.models import RelationCandidate, SkillRegistry


def _manifest(
    body: str,
    *,
    frontmatter: dict | None = None,
    relative_path: str = "example",
) -> RawSkillManifest:
    return RawSkillManifest(
        folder=SkillFolder(
            id_hint="example",
            path=Path("skills/example"),
            entry=Path("skills/example/SKILL.md"),
            relative_path=relative_path,
        ),
        frontmatter=frontmatter or {},
        body=body,
        body_sha256="digest",
    )


def test_schema_prompt_is_at_least_45_percent_shorter():
    assert len(_SCHEMA_EXTRACTION_PROMPT) <= 2498


def test_schema_context_preserves_non_empty_frontmatter_without_source_metadata():
    context = _build_llm_context(
        _manifest(
            "Full body.",
            frontmatter={
                "name": "example",
                "description": "Example skill.",
                "metadata": {"owner": "team", "empty": ""},
                "license": None,
                "tags": [],
            },
        )
    )

    assert context == {
        "frontmatter": {
            "name": "example",
            "description": "Example skill.",
            "metadata": {"owner": "team"},
        },
        "body": "Full body.",
    }


def test_schema_context_uses_short_ref_only_for_batch_correlation():
    context = _build_llm_context(
        _manifest("Full body.", relative_path="long/path/to/example"),
        skill_ref="s1",
    )

    assert context == {"skill_ref": "s1", "body": "Full body."}


def test_schema_context_only_marks_actual_truncation():
    full = _build_llm_context(_manifest("short"), body_limit=20)
    truncated = _build_llm_context(_manifest("long body"), body_limit=4)

    assert "body_truncated" not in full
    assert truncated == {"body": "long", "body_truncated": True}


def test_schema_warnings_are_limited_and_truncated():
    schema = schema_from_llm_payload(
        {
            "warnings": ["a" * 200, "second", "", "fourth"],
            "raw_output_notes": ["third", "ignored"],
        }
    )

    assert schema.warnings == ["a" * 160, "second", "fourth"]


def test_compact_schema_context_is_smaller_than_pretty_json():
    context = _build_llm_context(_manifest("body", frontmatter={"name": "example"}))
    compact = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    pretty = json.dumps(context, ensure_ascii=False, indent=2)

    assert len(compact) < len(pretty)


class _IOCaptureClient:
    def __init__(self) -> None:
        self.call = None

    async def complete_json_async(self, **kwargs):
        self.call = kwargs
        return json.dumps(
            {
                "resolutions": [
                    {
                        "id": "i1",
                        "action": "alias_existing",
                        "target": "content",
                        "confidence": 0.9,
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_io_resolution_uses_compact_ids_and_full_clean_vocabulary():
    resolver = LLMIONameResolver(LLMConfig(model="test"))
    client = _IOCaptureClient()
    resolver.client = client
    vocabulary = IONameVocabulary(
        version="v1",
        max_vocab_size=20,
        terms=[
            IONameVocabTerm(
                name="content",
                definition="Content value." + ("x" * 200),
                aliases={"body", "body", ""},
            )
        ],
    )
    candidate = IONameCandidate(
        raw_value="input_text",
        token="input_text",
        description="Text supplied by the caller.",
        skill_id="long-skill-id",
        direction="input",
        data_type="text",
    )

    result = await resolver.resolve_async([[candidate]], vocabulary)

    payload = json.loads(client.call["user_content"])
    assert payload == {
        "candidates": [
            {
                "id": "i1",
                "token": "input_text",
                "description": "Text supplied by the caller.",
                "direction": "input",
                "type": "text",
            }
        ],
        "vocabulary": [
            {
                "name": "content",
                "definition": ("Content value." + ("x" * 200))[:160],
                "aliases": ["body"],
            }
        ],
    }
    assert result["input_text"].normalized_value == "content"
    assert "\n" not in client.call["user_content"]


def test_io_resolution_reason_is_optional_and_truncated():
    vocabulary = IONameVocabulary(version="v1", max_vocab_size=20, terms=[])

    missing = _resolution_from_payload(
        {"action": "create_new", "target": "report", "confidence": 0.8},
        vocabulary,
    )
    long = _resolution_from_payload(
        {
            "action": "create_new",
            "target": "report",
            "confidence": 0.8,
            "reason": "r" * 200,
        },
        vocabulary,
    )

    assert missing.reason == ""
    assert long.reason == "r" * 160


def _graph_fixture():
    source_output = ArtifactSpec(
        name="report",
        type="markdown",
        description="Generated report." + ("o" * 200),
    )
    target_input = ParameterSpec(
        name="document",
        type="markdown",
        description="Document to review." + ("i" * 200),
    )
    source = Fingerprint(
        type="skill",
        id="source-with-a-long-id",
        name="Source",
        description="Produces a report." + ("s" * 300),
        version="1",
        inputs=[],
        outputs=[source_output],
    )
    target = Fingerprint(
        type="skill",
        id="target-with-a-long-id",
        name="Target",
        description="Reviews a report." + ("t" * 300),
        version="1",
        inputs=[target_input],
        outputs=[],
    )
    mapping = {
        "source_output": "report",
        "source_type": "markdown",
        "target_input": "document",
        "target_type": "markdown",
        "match_method": "semantic_overlap_match",
        "match_reason": "descriptions overlap",
    }
    candidate = RelationCandidate(
        source_id=source.id,
        target_id=target.id,
        relation_hints=["can_feed"],
        candidate_methods=["semantic_overlap_match"],
        priority="medium",
        evidence={
            "directions": {
                f"{source.id}->{target.id}": {
                    "source_outputs": [source_output.to_dict()],
                    "target_inputs": [target_input.to_dict()],
                    "port_mappings": [mapping],
                    "matched_terms": ["report"],
                }
            }
        },
    )
    return SkillRegistry(skills={source.id: source, target.id: target}), candidate


def test_graph_context_only_contains_compact_candidate_evidence():
    registry, candidate = _graph_fixture()

    context = graph_prompt.build_llm_context(registry, [candidate])

    assert set(context) == {"candidates"}
    item = context["candidates"][0]
    assert item["id"] == "c1"
    assert item["source"] == {
        "name": "Source",
        "description": registry.skills[candidate.source_id].description[:240],
    }
    assert item["target"] == {
        "name": "Target",
        "description": registry.skills[candidate.target_id].description[:240],
    }
    forward = item["directions"]["forward"]
    assert forward["outputs"] == [{
        "name": "report",
        "type": "markdown",
        "description": registry.skills[candidate.source_id].outputs[0].description[:160],
    }]
    assert forward["inputs"] == [{
        "name": "document",
        "type": "markdown",
        "required": True,
        "description": registry.skills[candidate.target_id].inputs[0].description[:160],
    }]
    assert forward["ports"] == [{
        "output": "report",
        "output_type": "markdown",
        "input": "document",
        "input_type": "markdown",
    }]
    assert not {
        "input_sha256",
        "allowed_relation_types",
        "priority",
        "candidate_methods",
    } & set(item)


def test_graph_compact_response_is_expanded_from_candidate_evidence():
    registry, candidate = _graph_fixture()
    expand = getattr(graph_prompt, "expand_compact_llm_response", None)

    assert callable(expand)
    payload, diagnostics = expand(
        {
            "matches": [
                {
                    "id": "c1",
                    "direction": "forward",
                    "confidence": 0.92,
                    "reason": "r" * 200,
                }
            ]
        },
        [candidate],
    )

    assert diagnostics == []
    match = payload["matches"][0]
    assert match == {
        "candidate_id": candidate.key,
        "source_id": candidate.source_id,
        "target_id": candidate.target_id,
        "relation_type": "can_feed",
        "confidence": 0.92,
        "method": "llm_ontology_match",
        "reasons": ["r" * 160],
        "supporting_fields": {
            "port_mappings": [{"source_output": "report", "target_input": "document"}],
            "source_outputs": ["report"],
            "target_inputs": ["document"],
        },
    }


def test_graph_compact_response_reports_unknown_and_duplicate_ids():
    _, candidate = _graph_fixture()
    expand = getattr(graph_prompt, "expand_compact_llm_response", None)

    assert callable(expand)
    payload, diagnostics = expand(
        {
            "matches": [
                {"id": "unknown", "direction": "forward", "confidence": 0.8},
                {"id": "c1", "direction": "forward", "confidence": 0.8},
                {"id": "c1", "direction": "forward", "confidence": 0.9},
            ]
        },
        [candidate],
    )

    assert len(payload["matches"]) == 1
    assert [item.code for item in diagnostics] == [
        "unknown_candidate_id",
        "duplicate_candidate_id",
    ]


def test_graph_context_supports_legacy_direct_candidate_evidence_shape():
    registry, candidate = _graph_fixture()
    direct_candidate = RelationCandidate(
        source_id=candidate.source_id,
        target_id=candidate.target_id,
        relation_hints=candidate.relation_hints,
        candidate_methods=candidate.candidate_methods,
        priority=candidate.priority,
        evidence=candidate.evidence["directions"][candidate.key],
    )

    context = graph_prompt.build_llm_context(registry, [direct_candidate])

    assert "forward" in context["candidates"][0]["directions"]


def test_graph_context_is_at_least_50_percent_smaller_than_previous_shape():
    registry, candidate = _graph_fixture()
    compact = graph_prompt.build_llm_context(registry, [candidate])
    previous = {
        "allowed_relation_types": ["can_feed"],
        "skills": [
            registry.skills[candidate.source_id].graph_identity_dict(),
            registry.skills[candidate.target_id].graph_identity_dict(),
        ],
        "candidates": [candidate.to_dict()],
        "input_sha256": "x" * 64,
    }

    compact_size = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    previous_size = len(json.dumps(previous, ensure_ascii=False, separators=(",", ":")))

    assert compact_size <= previous_size * 0.5


def test_prompt_protocol_versions_invalidate_fingerprint_and_relation_caches(
    monkeypatch,
):
    config = symphony_config_from_dict({})
    llm_config = LLMConfig(model="test")
    first = build_module._fingerprint_signature(config, llm_config)
    monkeypatch.setattr(
        build_module,
        "SCHEMA_EXTRACTION_PROTOCOL_VERSION",
        "symphony-schema-extraction-v3",
    )
    second = build_module._fingerprint_signature(config, llm_config)
    matcher = OpenAICompatibleOntologyMatcher(llm_config)

    assert first != second
    assert matcher.prompt_version == "Orchestration-graph-match-v2"
