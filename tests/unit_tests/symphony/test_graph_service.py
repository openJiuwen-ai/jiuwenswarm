import asyncio
import json

import pytest

from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ExtractedSkillSchema,
    ParameterSpec,
    RawSkillManifest,
    Fingerprint,
    SkillFolder,
)
from jiuwenswarm.symphony.fingerprint.normalize import (
    IONameCandidate,
    IONameResolution,
    IONameVocabulary,
    LLMIONameResolver,
    SkillFingerprintNormalizer,
)
from jiuwenswarm.symphony.fingerprint.extract.extractor import LLMSchemaExtractor
from jiuwenswarm.symphony.fingerprint.manifest import SkillManifestParser
from jiuwenswarm.symphony.fingerprint.pipeline import FingerprintExtractor
from jiuwenswarm.symphony.fingerprint.scan import SkillFolderScanner
from jiuwenswarm.symphony.fingerprint.artifacts import write_extraction_result
from jiuwenswarm.symphony.graph import (
    GraphBuilder,
    GraphDiagnostic,
    LLMMatch,
    OpenAICompatibleOntologyMatcher,
    RelationCandidate,
    SkillRegistry,
)
from jiuwenswarm.symphony.graph.candidates.generator import CandidateGenerator
from jiuwenswarm.symphony.score_state import (
    ScoreState,
    ScoreStateEntry,
    write_score_state,
)
from jiuwenswarm.symphony.build import (
    ScoreBuildRuntimeFactory,
    build_score,
    score_status,
    _fingerprint_signature,
)
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.graph.matcher.cache import CachedOntologyMatcher
from jiuwenswarm.symphony.orchestration.artifacts import load_score_artifacts


class _SchemaExtractor:
    def __init__(self):
        self.extract_count = 0

    async def extract(self, manifest):
        self.extract_count += 1
        return ExtractedSkillSchema(
            description=f"{manifest.folder.id_hint} schema",
            inputs=[ParameterSpec(name="input", type="text")],
            outputs=[ArtifactSpec(name="result", type="text")],
        )


class _LinkedSchemaExtractor:
    def __init__(self):
        self.extract_count = 0

    async def extract(self, manifest):
        self.extract_count += 1
        if manifest.folder.id_hint == "skill-1":
            return ExtractedSkillSchema(
                description="Create a draft.",
                inputs=[ParameterSpec(name="brief", type="text")],
                outputs=[ArtifactSpec(name="draft", type="markdown")],
            )
        return ExtractedSkillSchema(
            description="Review a draft.",
            inputs=[ParameterSpec(name="draft", type="markdown")],
            outputs=[ArtifactSpec(name="review", type="markdown")],
        )


class _NoopMatcher:
    thresholds = {"can_feed": 0.7}

    async def match(self, registry, candidates):
        return []

    @staticmethod
    def manifest_metadata():
        return {"matcher": "noop"}


class _CountingAcceptedMatcher:
    thresholds = {"can_feed": 0.7}

    def __init__(self):
        self.calls = []

    async def match(self, registry, candidates):
        del registry
        candidates = list(candidates)
        self.calls.append([candidate.key for candidate in candidates])
        return [
            LLMMatch(
                source_id=candidate.source_id,
                target_id=candidate.target_id,
                relation_type="can_feed",
                confidence=0.9,
                accepted=True,
                candidate_id=candidate.key,
            )
            for candidate in candidates
        ]

    @staticmethod
    def manifest_metadata():
        return {"matcher": "counting-accepted"}


class _MismatchedBatchSchemaExtractor(_SchemaExtractor):
    use_batch = True
    batch_size = 2

    async def extract_many(self, manifests):
        return [await self.extract(manifests[0])]


class _NonBatchSchemaExtractor(_SchemaExtractor):
    use_batch = False
    batch_size = 99

    async def extract_many(self, manifests):
        raise AssertionError("Non-batch extractor should not use extract_many.")


class _BatchSchemaExtractor:
    use_batch = True

    def __init__(self, batch_size=2):
        self.batch_size = batch_size
        self.batches = []

    async def extract(self, manifest):
        raise AssertionError("Batch extractor should use extract_many.")

    async def extract_many(self, manifests):
        self.batches.append([manifest.folder.id_hint for manifest in manifests])
        return [
            ExtractedSkillSchema(
                description=f"{manifest.folder.id_hint} schema",
                inputs=[ParameterSpec(name="input", type="text")],
                outputs=[ArtifactSpec(name="result", type="text")],
            )
            for manifest in manifests
        ]


class _PromptBatchIONameResolver:
    batch_size = 2

    def __init__(self):
        self.calls = []

    async def resolve_async(self, candidates_by_skill, vocabulary):
        del vocabulary
        self.calls.append([[candidate.token for candidate in batch] for batch in candidates_by_skill])
        candidates = [
            candidate
            for batch in reversed(candidates_by_skill)
            for candidate in reversed(batch)
        ]
        return {
            candidate.token: IONameResolution(
                action="create_new",
                normalized_value=candidate.token,
                confidence=0.9,
                reason="test",
            )
            for candidate in candidates
        }


class _CreateNewIONameResolver:
    batch_size = 1

    def __init__(self):
        self.calls = []

    async def resolve_async(self, candidates_by_skill, vocabulary):
        del vocabulary
        self.calls.append([[candidate.token for candidate in batch] for batch in candidates_by_skill])
        return {
            candidate.token: IONameResolution(
                action="create_new",
                normalized_value=candidate.token,
                confidence=0.9,
                reason="test",
            )
            for batch in candidates_by_skill
            for candidate in batch
        }


class _ExcludeFromVocabIONameResolver:
    batch_size = 1

    async def resolve_async(self, candidates_by_skill, vocabulary):
        del vocabulary
        return {
            candidate.token: IONameResolution(
                action="exclude_from_vocab",
                normalized_value=None,
                confidence=0.9,
                reason="not a vocabulary term",
            )
            for batch in candidates_by_skill
            for candidate in batch
        }


class _MappingIONameResolver:
    def __init__(self, resolutions, batch_size=1):
        self.resolutions = resolutions
        self.batch_size = batch_size
        self.calls = []

    async def resolve_async(self, candidates_by_skill, vocabulary):
        del vocabulary
        self.calls.append(
            [[candidate.token for candidate in batch] for batch in candidates_by_skill]
        )
        return {
            candidate.token: self.resolutions[candidate.token]
            for batch in candidates_by_skill
            for candidate in batch
        }


class _CaptureJSONClient:
    def __init__(self):
        self.calls = []

    async def complete_json_async(self, **kwargs):
        self.calls.append(kwargs)
        return """
        {
          "resolutions": [
            {
              "id": "i1",
              "action": "alias_existing",
              "target": "content",
              "confidence": 0.9,
              "reason": "same content role",
              "definition": "Text content alias"
            }
          ]
        }
        """


class _CaptureSchemaClient:
    def __init__(self):
        self.calls = []

    async def complete_json_async(self, **kwargs):
        self.calls.append({"method": "one", **kwargs})
        payload = json.loads(kwargs["user_content"])
        if isinstance(payload.get("skills"), list):
            return json.dumps(
                {
                    "schemas": [
                        {
                            "skill_ref": item["skill_ref"],
                            "description": "Schema",
                            "inputs": [{"name": "input", "type": "text"}],
                            "outputs": [{"name": "result", "type": "text"}],
                            "confidence": 0.9,
                            "warnings": [],
                        }
                        for item in payload["skills"]
                    ]
                }
            )
        return json.dumps(
            {
                "description": "Schema",
                "inputs": [{"name": "input", "type": "text"}],
                "outputs": [{"name": "result", "type": "text"}],
                "confidence": 0.9,
                "warnings": [],
            }
        )

    async def complete_json_many_async(self, requests, **kwargs):
        self.calls.append({"method": "many", "requests": requests, **kwargs})
        return [
            json.dumps(
                {
                    "description": "Schema",
                    "inputs": [{"name": "input", "type": "text"}],
                    "outputs": [{"name": "result", "type": "text"}],
                    "confidence": 0.9,
                    "warnings": [],
                }
            )
            for _ in requests
        ]


class _CaptureMatchClient:
    def __init__(self):
        self.calls = []

    async def complete_json_async(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(
            {
                "matches": [
                    {
                        "id": "c1",
                        "direction": "forward",
                        "confidence": 0.95,
                        "reason": "result satisfies input",
                    }
                ]
            }
        )


class _OutOfOrderBatchMatcher:
    def __init__(self):
        self.batch_size = 1
        self.max_workers = 3
        self.require_consensus = False
        self.thresholds = {"can_feed": 0.7}
        self.diagnostics = []
        self.progress = None

    async def match(self, registry, candidates):
        del registry
        candidate_list = list(candidates)
        batches = [
            (batch_index, [candidate])
            for batch_index, candidate in enumerate(candidate_list, start=1)
        ]
        results = await asyncio.gather(
            *(
                self._match_batch(None, batch, batch_index, len(batches))
                for batch_index, batch in batches
            )
        )
        matches = []
        for _, batch_matches, batch_diagnostics in sorted(
            results,
            key=lambda item: item[0],
        ):
            matches.extend(batch_matches)
            self.diagnostics.extend(batch_diagnostics)
        return matches

    async def _match_batch(self, registry, batch, batch_index, total_batches):
        del registry, batch, total_batches
        await asyncio.sleep(0.03 if batch_index == 1 else 0)
        return (
            batch_index,
            [
                LLMMatch(
                    source_id=f"source-{batch_index}",
                    target_id=f"target-{batch_index}",
                    relation_type="can_feed",
                    confidence=0.9,
                    accepted=True,
                    candidate_id=f"candidate-{batch_index}",
                )
            ],
            [
                GraphDiagnostic(
                    stage="matching",
                    severity="info",
                    code="batch_done",
                    message=f"batch {batch_index}",
                )
            ],
        )


def _raw_manifest(tmp_path, folder_name):
    folder_path = tmp_path / folder_name
    folder_path.mkdir()
    skill_file = folder_path / "SKILL.md"
    skill_file.write_text(f"---\nname: {folder_name}\n---\n\nTest.", encoding="utf-8")
    return RawSkillManifest(
        folder=SkillFolder(
            id_hint=folder_name,
            path=folder_path,
            entry=skill_file,
            relative_path=folder_name,
        ),
        frontmatter={"name": folder_name},
        body="Test.",
        body_sha256=folder_name,
    )


def _expected_thinking_disabled_overrides():
    return {
        "extra_body": {
            "thinking": {"type": "disabled"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }


@pytest.mark.asyncio
async def test_schema_extractor_disables_thinking_for_single_extract(monkeypatch, tmp_path):
    client = _CaptureSchemaClient()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.fingerprint.extract.extractor.create_llm_client",
        lambda config: client,
    )
    extractor = LLMSchemaExtractor(
        LLMConfig(
            model="model-a",
            model_client_config={
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "client_provider": "openai",
            },
        )
    )

    result = await extractor.extract(_raw_manifest(tmp_path, "schema-single"))

    assert result.outputs[0].name == "result"
    assert client.calls[0]["request_overrides"] == _expected_thinking_disabled_overrides()
    assert "Return JSON only" in client.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_schema_extractor_disables_thinking_for_many_paths(monkeypatch, tmp_path):
    client = _CaptureSchemaClient()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.fingerprint.extract.extractor.create_llm_client",
        lambda config: client,
    )
    config = LLMConfig(
        model="model-a",
        model_client_config={
            "api_key": "key",
            "api_base": "https://example.test/v1",
            "client_provider": "openai",
        },
    )

    await LLMSchemaExtractor(config, batch_size=1).extract_many(
        [_raw_manifest(tmp_path, "schema-many-one")]
    )
    await LLMSchemaExtractor(config, batch_size=2).extract_many(
        [
            _raw_manifest(tmp_path, "schema-prompt-a"),
            _raw_manifest(tmp_path, "schema-prompt-b"),
        ]
    )

    assert client.calls[0]["method"] == "many"
    assert client.calls[0]["request_overrides"] == _expected_thinking_disabled_overrides()
    assert client.calls[1]["method"] == "one"
    assert client.calls[1]["request_overrides"] == _expected_thinking_disabled_overrides()
    assert "Return JSON only" in client.calls[1]["system_prompt"]


@pytest.mark.asyncio
async def test_graph_matcher_disables_thinking_for_forward_and_reverse(monkeypatch):
    client = _CaptureMatchClient()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.graph.matcher.openai.create_llm_client",
        lambda config: client,
    )
    matcher = OpenAICompatibleOntologyMatcher(
        LLMConfig(
            model="model-a",
            model_client_config={
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "client_provider": "openai",
            },
        ),
        batch_size=1,
        require_consensus=True,
    )
    registry = SkillRegistry(
        skills={
            "source": Fingerprint(
                type="skill",
                id="source",
                name="Source",
                description="Produces result",
                version="1.0.0",
                inputs=[],
                outputs=[ArtifactSpec(name="result", type="text")],
            ),
            "target": Fingerprint(
                type="skill",
                id="target",
                name="Target",
                description="Consumes input",
                version="1.0.0",
                inputs=[ParameterSpec(name="input", type="text")],
                outputs=[],
            ),
        }
    )
    candidate = RelationCandidate(
        source_id="source",
        target_id="target",
        relation_hints=["can_feed"],
        candidate_methods=["test"],
        priority="high",
        evidence={
            "directions": {
                "source->target": {
                    "port_mappings": [
                        {
                            "source_output": "result",
                            "target_input": "input",
                        }
                    ],
                    "source_outputs": [{"name": "result"}],
                    "target_inputs": [{"name": "input"}],
                }
            }
        },
    )

    matches = await matcher.match(registry, [candidate])

    assert len(matches) == 1
    assert len(client.calls) == 2
    assert [
        call["request_overrides"]
        for call in client.calls
    ] == [
        _expected_thinking_disabled_overrides(),
        _expected_thinking_disabled_overrides(),
    ]
    assert all("Return JSON only" in call["system_prompt"] for call in client.calls)


@pytest.mark.asyncio
async def test_build_score_fingerprint_progress_does_not_exceed_total(tmp_path):
    skills_root = tmp_path / "skills"
    for index in range(3):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )

    build_log = []

    await build_score(
        skills_root,
        tmp_path / "score",
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
        build_log=lambda stage, **details: build_log.append({"stage": stage, **details}),
    )

    for stage in (
        "fingerprint.parse.start",
        "fingerprint.extract.start",
        "fingerprint.normalize.start",
    ):
        entries = [entry for entry in build_log if entry["stage"] == stage]
        assert [entry["current"] for entry in entries] == [1, 2, 3]
        assert all(entry["total"] == 3 for entry in entries)


@pytest.mark.asyncio
async def test_build_score_passes_separate_symphony_stage_configs(tmp_path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "skill-1"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Skill 1\n---\n\nTest skill 1.",
        encoding="utf-8",
    )
    runtime_config = symphony_config_from_dict(
        {
            "fingerprint": {
                "extraction": {
                    "workers": 2,
                    "batch_size": 3,
                    "body_limit": 5000,
                },
                "normalization": {
                    "workers": 5,
                    "batch_size": 6,
                    "duplicate_name_similarity_threshold": 0.42,
                    "max_vocab_size": 7,
                },
            },
            "build": {
                "workers": 8,
                "batch_size": 9,
                "max_candidates_per_skill_relation": 17,
                "require_consensus": False,
                "min_edge_confidence": 0.33,
            },
        }
    )
    seen = {}

    class _RuntimeFactory:
        @staticmethod
        def schema_extractor(llm_config, extraction_config):
            seen["extraction"] = extraction_config
            return _SchemaExtractor()

        @staticmethod
        def io_name_resolver(llm_config, normalization_config):
            seen["normalization"] = normalization_config
            return _CreateNewIONameResolver()

        @staticmethod
        def matcher(llm_config, *, build_config, build_log=None):
            seen["build"] = build_config
            return _NoopMatcher()

    await build_score(
        skills_root,
        tmp_path / "score",
        llm_config=LLMConfig(
            model="model",
            model_client_config={
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "client_provider": "openai",
            },
        ),
        symphony_config=runtime_config,
        runtime_factory=_RuntimeFactory(),
    )

    assert seen["extraction"].workers == 2
    assert seen["extraction"].batch_size == 3
    assert seen["extraction"].body_limit == 5000
    assert seen["normalization"].workers == 5
    assert seen["normalization"].batch_size == 6
    assert seen["normalization"].duplicate_name_similarity_threshold == 0.42
    assert seen["normalization"].max_vocab_size == 7
    assert seen["build"].workers == 8
    assert seen["build"].batch_size == 9
    assert seen["build"].max_candidates_per_skill_relation == 17
    assert seen["build"].require_consensus is False
    assert seen["build"].min_edge_confidence == 0.33


def test_default_matcher_uses_configured_min_edge_confidence():
    runtime_config = symphony_config_from_dict(
        {"build": {"min_edge_confidence": 0.42}}
    )
    matcher = ScoreBuildRuntimeFactory().matcher(
        LLMConfig(
            model="model",
            model_client_config={
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "client_provider": "openai",
            },
        ),
        build_config=runtime_config.build,
    )

    assert matcher.thresholds == {"can_feed": 0.42}


def test_llm_endpoint_does_not_change_fingerprint_cache_signature():
    runtime_config = symphony_config_from_dict({})
    first_signature = _fingerprint_signature(
        runtime_config,
        LLMConfig(
            model="model",
            model_client_config={
                "api_key": "key-a",
                "api_base": "https://a.example.test/v1",
                "client_provider": "openai",
            },
        ),
    )
    second_signature = _fingerprint_signature(
        runtime_config,
        LLMConfig(
            model="model",
            model_client_config={
                "api_key": "key-b",
                "api_base": "https://b.example.test/v1",
                "client_provider": "openai",
            },
        ),
    )

    assert first_signature == second_signature


def test_llm_endpoint_is_not_recorded_in_relation_cache_signature(tmp_path):
    matcher = ScoreBuildRuntimeFactory().matcher(
        LLMConfig(
            model="model",
            model_client_config={
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "client_provider": "openai",
            },
        ),
        build_config=symphony_config_from_dict({}).build,
    )

    metadata = matcher.manifest_metadata()
    cached_matcher = CachedOntologyMatcher(
        matcher,
        tmp_path / "relation_matches.json",
        fingerprints=[],
    )

    assert "base_url" not in metadata
    assert "api_key" not in metadata
    assert "base_url" not in cached_matcher.cache.matcher_signature
    assert "api_key" not in cached_matcher.cache.matcher_signature


@pytest.mark.asyncio
async def test_build_score_uses_configured_scan_max_depth(tmp_path):
    skills_root = tmp_path / "skills"
    shallow_skill = skills_root / "skill-1"
    nested_skill = skills_root / "nested" / "skill-2"
    shallow_skill.mkdir(parents=True)
    nested_skill.mkdir(parents=True)
    (shallow_skill / "SKILL.md").write_text(
        "---\nname: Skill 1\n---\n\nShallow skill.",
        encoding="utf-8",
    )
    (nested_skill / "SKILL.md").write_text(
        "---\nname: Skill 2\n---\n\nNested skill.",
        encoding="utf-8",
    )
    runtime_config = symphony_config_from_dict(
        {"fingerprint": {"scan": {"max_depth": 1}}}
    )

    status = score_status(
        skills_root,
        tmp_path / "score",
        symphony_config=runtime_config,
    )
    result = await build_score(
        skills_root,
        tmp_path / "score",
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
        symphony_config=runtime_config,
    )

    assert status.skill_count == 1
    assert result.skill_count == 1
    assert result.extracted_count == 1


@pytest.mark.asyncio
async def test_build_score_reuses_unchanged_and_reextracts_changed_skills(tmp_path):
    skills_root = tmp_path / "skills"
    score_dir = tmp_path / "score"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )

    first = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
    )
    second = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
    )
    (skills_root / "skill-2" / "SKILL.md").write_text(
        "---\nname: Skill 2\n---\n\nChanged skill 2.",
        encoding="utf-8",
    )
    third = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
    )
    fourth = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
        force=True,
    )

    assert first.extracted_count == 2
    assert first.reused_count == 0
    assert second.extracted_count == 0
    assert second.reused_count == 2
    assert third.extracted_count == 1
    assert third.reused_count == 1
    assert fourth.extracted_count == 2
    assert fourth.reused_count == 0


@pytest.mark.asyncio
async def test_build_score_publishes_versioned_artifacts_and_loads_current(tmp_path):
    skills_root = tmp_path / "skills"
    score_dir = tmp_path / "score"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )

    result = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_LinkedSchemaExtractor(),
        matcher=_CountingAcceptedMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
    )
    artifacts = load_score_artifacts(score_dir)

    assert result.version
    assert (score_dir / "current.json").is_file()
    assert (score_dir / "versions" / result.version / "score_manifest.json").is_file()
    assert artifacts.score_dir == (score_dir / "versions" / result.version).resolve()
    assert [skill["id"] for skill in artifacts.skills] == ["skill-1", "skill-2"]


@pytest.mark.asyncio
async def test_build_score_reuses_unchanged_relation_matches(tmp_path):
    skills_root = tmp_path / "skills"
    score_dir = tmp_path / "score"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )

    first_matcher = _CountingAcceptedMatcher()
    first = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_LinkedSchemaExtractor(),
        matcher=first_matcher,
        io_name_resolver=_CreateNewIONameResolver(),
    )
    second_matcher = _CountingAcceptedMatcher()
    second = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_LinkedSchemaExtractor(),
        matcher=second_matcher,
        io_name_resolver=_CreateNewIONameResolver(),
    )

    assert first.relation_resolved_count >= 1
    assert first.relation_reused_count == 0
    assert first_matcher.calls
    assert second.relation_resolved_count == 0
    assert second.relation_reused_count == first.relation_resolved_count
    assert second_matcher.calls == []


@pytest.mark.asyncio
async def test_relation_cache_preserves_matcher_worker_windows(tmp_path):
    matcher = _CountingAcceptedMatcher()
    matcher.batch_size = 2
    matcher.max_workers = 3
    cached_matcher = CachedOntologyMatcher(
        matcher,
        tmp_path / "relation_matches.json",
        fingerprints=[],
    )
    candidates = [
        RelationCandidate(
            source_id=f"source-{index}",
            target_id=f"target-{index}",
            relation_hints=["can_feed"],
            candidate_methods=["test"],
            priority="medium",
        )
        for index in range(8)
    ]

    first = await cached_matcher.match(SkillRegistry(skills={}), candidates)
    second = await cached_matcher.match(SkillRegistry(skills={}), candidates)

    assert [len(call) for call in matcher.calls] == [6, 2]
    assert len(first) == 8
    assert len(second) == 8
    assert cached_matcher.stats.reused_count == 8
    assert cached_matcher.stats.resolved_count == 0


@pytest.mark.asyncio
async def test_graph_manifest_records_candidate_generation_limit():
    result = await GraphBuilder(
        matcher=_NoopMatcher(),
        candidate_generator=CandidateGenerator(
            max_candidates_per_skill_relation=17,
            max_port_mappings_per_candidate=9,
            max_exact_io_pair_fanout=33,
        ),
    ).build([])

    assert result.manifest.candidate_generation == {
        "max_candidates_per_skill_relation": 17,
        "max_port_mappings_per_candidate": 9,
        "max_exact_io_pair_fanout": 33,
    }


@pytest.mark.asyncio
async def test_build_score_reports_removed_skills(tmp_path):
    skills_root = tmp_path / "skills"
    score_dir = tmp_path / "score"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )
    await build_score(
        skills_root,
        score_dir,
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
    )
    (skills_root / "skill-2" / "SKILL.md").unlink()
    (skills_root / "skill-2").rmdir()

    result = await build_score(
        skills_root,
        score_dir,
        schema_extractor=_SchemaExtractor(),
        matcher=_NoopMatcher(),
        io_name_resolver=_CreateNewIONameResolver(),
    )

    assert result.skill_count == 1
    assert result.removed_count == 1
    assert result.reused_count == 1
    assert result.extracted_count == 0


def _write_state_from_extraction_result(result, output_dir):
    entries = {}
    for folder in result.folders:
        fingerprint = result.fingerprints_by_path[folder.relative_path]
        entries[folder.relative_path] = ScoreStateEntry(
            skill_id=fingerprint.id,
            relative_path=folder.relative_path,
            skill_md_sha256=result.current_hashes[folder.relative_path],
            fingerprint_hash="test-fingerprint-hash",
            status="active",
            updated_at="2026-06-06T00:00:00+00:00",
        )
    write_score_state(ScoreState(skills=entries), output_dir)


@pytest.mark.asyncio
async def test_fingerprint_extractor_extract_from_root_preserves_changed_folder_progress_index(tmp_path):
    skills_root = tmp_path / "skills"
    output_dir = tmp_path / "score"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )
    first = await FingerprintExtractor(
        schema_extractor=_SchemaExtractor(),
        parser=SkillManifestParser(),
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(skills_root, output_dir=output_dir)
    write_extraction_result(first, output_dir)
    _write_state_from_extraction_result(first, output_dir)
    (skills_root / "skill-2" / "SKILL.md").write_text(
        "---\nname: Skill 2\n---\n\nChanged skill 2.",
        encoding="utf-8",
    )

    progress = []
    schema_extractor = _SchemaExtractor()
    extractor = FingerprintExtractor(
        schema_extractor=schema_extractor,
        parser=SkillManifestParser(),
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
        progress=lambda stage, current, total, item: progress.append(
            {
                "stage": stage,
                "current": current,
                "total": total,
                "item": item,
            }
        ),
    )

    result = await extractor.extract_from_root(skills_root, output_dir=output_dir)

    assert result.extracted_count == 1
    assert result.reused_count == 1
    assert result.fingerprints_by_path["skill-2"].id == "skill-2"
    assert schema_extractor.extract_count == 1
    assert [
        (entry["stage"], entry["current"], entry["total"])
        for entry in progress
    ] == [
        ("parse", 2, 2),
        ("extract", 2, 2),
        ("normalize", 2, 2),
    ]


@pytest.mark.asyncio
async def test_fingerprint_extractor_reuses_per_skill_cache_without_published_state(tmp_path):
    skills_root = tmp_path / "skills"
    output_dir = tmp_path / "score"
    skill_dir = skills_root / "skill-1"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Skill 1\n---\n\nTest skill 1.",
        encoding="utf-8",
    )

    first_schema_extractor = _SchemaExtractor()
    first = await FingerprintExtractor(
        schema_extractor=first_schema_extractor,
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(
        skills_root,
        output_dir=output_dir,
        fingerprint_signature="test-signature",
    )
    second_schema_extractor = _SchemaExtractor()
    second = await FingerprintExtractor(
        schema_extractor=second_schema_extractor,
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(
        skills_root,
        output_dir=output_dir,
        fingerprint_signature="test-signature",
    )

    assert first.extracted_count == 1
    assert first.reused_count == 0
    assert second.extracted_count == 0
    assert second.reused_count == 1
    assert second_schema_extractor.extract_count == 0


@pytest.mark.asyncio
async def test_fingerprint_extractor_batch_schema_count_mismatch_raises(tmp_path):
    skills_root = tmp_path / "skills"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )
    extractor = FingerprintExtractor(
        schema_extractor=_MismatchedBatchSchemaExtractor(),
        parser=SkillManifestParser(),
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    )

    with pytest.raises(RuntimeError, match="Batch schema extractor returned"):
        await extractor.extract_from_root(skills_root, output_dir=tmp_path / "score")


@pytest.mark.asyncio
async def test_fingerprint_extractor_uses_schema_extractor_batches(tmp_path):
    skills_root = tmp_path / "skills"
    for index in range(3):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )
    progress = []
    schema_extractor = _BatchSchemaExtractor(batch_size=2)

    result = await FingerprintExtractor(
        schema_extractor=schema_extractor,
        parser=SkillManifestParser(),
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
        progress=lambda stage, current, total, item: progress.append(
            {
                "stage": stage,
                "current": current,
                "total": total,
                "item": item,
            }
        ),
    ).extract_from_root(skills_root, output_dir=tmp_path / "score")

    assert schema_extractor.batches == [["skill-1", "skill-2"], ["skill-3"]]
    assert result.extracted_count == 3
    assert [
        (entry["current"], entry["total"])
        for entry in progress
        if entry["stage"] == "extract"
    ] == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.asyncio
async def test_fingerprint_extractor_non_batch_extractor_uses_single_item_batches(tmp_path):
    skills_root = tmp_path / "skills"
    for index in range(3):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )
    schema_extractor = _NonBatchSchemaExtractor()

    result = await FingerprintExtractor(
        schema_extractor=schema_extractor,
        parser=SkillManifestParser(),
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
        max_workers=2,
    ).extract_from_root(skills_root, output_dir=tmp_path / "score")

    assert result.extracted_count == 3
    assert schema_extractor.extract_count == 3


@pytest.mark.asyncio
async def test_normalizer_async_prefetch_keeps_result_order_stable(tmp_path):
    resolver = _PromptBatchIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)
    items = [
        (
            _raw_manifest(tmp_path, "skill-1"),
            ExtractedSkillSchema(
                outputs=[ArtifactSpec(name="first_result", type="text")],
            ),
        ),
        (
            _raw_manifest(tmp_path, "skill-2"),
            ExtractedSkillSchema(
                outputs=[ArtifactSpec(name="second_result", type="text")],
            ),
        ),
    ]

    results = await normalizer.normalize(items)

    assert [result.fingerprint.id for result in results] == ["skill-1", "skill-2"]
    assert [result.fingerprint.outputs[0].name for result in results] == [
        "first_result",
        "second_result",
    ]
    assert resolver.calls == [[["first_result"], ["second_result"]]]


@pytest.mark.asyncio
async def test_normalizer_batch_size_one_uses_single_skill_batches(tmp_path):
    resolver = _CreateNewIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)
    items = [
        (
            _raw_manifest(tmp_path, "skill-1"),
            ExtractedSkillSchema(outputs=[ArtifactSpec(name="first_result", type="text")]),
        ),
        (
            _raw_manifest(tmp_path, "skill-2"),
            ExtractedSkillSchema(outputs=[ArtifactSpec(name="second_result", type="text")]),
        ),
    ]

    await normalizer.normalize(items)

    assert resolver.calls == [[["first_result"]], [["second_result"]]]


@pytest.mark.asyncio
async def test_normalizer_overrides_single_image_reference_types(tmp_path):
    normalizer = SkillFingerprintNormalizer(io_name_resolver=_CreateNewIONameResolver())

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "image-skill"),
        ExtractedSkillSchema(
            inputs=[
                ParameterSpec(
                    name="imageUrl",
                    type="url",
                    description="Public image URL to process.",
                ),
                ParameterSpec(
                    name="image",
                    type="text",
                    description="图片输入，可以是图片 URL、本地文件路径或图片 BASE64 编码数据。",
                ),
                ParameterSpec(
                    name="source_language",
                    type="text",
                    required=False,
                    description="Source language of the text in the image.",
                ),
                ParameterSpec(
                    name="homepage_url",
                    type="url",
                    description="Public product homepage URL.",
                ),
                ParameterSpec(
                    name="download_url",
                    type="url",
                    description="Download URL for a file artifact.",
                ),
            ],
            outputs=[
                ArtifactSpec(
                    name="translated_image_url",
                    type="url",
                    description="URL of the translated image.",
                )
            ],
        ),
    )

    input_types = {item.name: item.type for item in result.fingerprint.inputs}
    output_types = {item.name: item.type for item in result.fingerprint.outputs}
    assert input_types["imageurl"] == "image"
    assert input_types["image"] == "image"
    assert input_types["source_language"] == "text"
    assert input_types["homepage_url"] == "url"
    assert input_types["download_url"] == "url"
    assert output_types["translated_image_url"] == "image"
    override_decisions = [
        decision
        for decision in result.decisions
        if decision.field == "type" and decision.method == "semantic_media_override"
    ]
    assert {decision.normalized_value for decision in override_decisions} == {"image"}
    assert {decision.raw_value for decision in override_decisions} == {"url", "text"}


@pytest.mark.asyncio
async def test_normalizer_resolves_single_skill_candidates_together(tmp_path):
    resolver = _CreateNewIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)

    await normalizer.normalize_single(
        _raw_manifest(tmp_path, "skill"),
        ExtractedSkillSchema(
            inputs=[ParameterSpec(name="source_text", type="text")],
            outputs=[ArtifactSpec(name="summary_text", type="text")],
        ),
    )

    assert resolver.calls == [[["source_text", "summary_text"]]]


@pytest.mark.asyncio
async def test_normalizer_aliases_natural_language_command_input_to_text(tmp_path):
    resolver = _CreateNewIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "calendar-memo"),
        ExtractedSkillSchema(
            inputs=[
                ParameterSpec(
                    name="command",
                    type="text",
                    description="用户指令，如 '添加 明天下午3点 团队周会'",
                )
            ],
            outputs=[ArtifactSpec(name="result", type="markdown")],
        ),
    )

    assert resolver.calls == [[["result"]]]
    assert result.fingerprint.inputs[0].name == "text"
    assert normalizer.io_name_vocabulary.lookup("command") == "text"
    name_decisions = [
        decision
        for decision in result.decisions
        if decision.field == "name" and decision.token == "command"
    ]
    assert len(name_decisions) == 1
    assert name_decisions[0].method == "semantic_alias"
    assert name_decisions[0].normalized_value == "text"


@pytest.mark.asyncio
async def test_normalizer_keeps_control_command_input_as_command(tmp_path):
    resolver = _CreateNewIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "cli-skill"),
        ExtractedSkillSchema(
            inputs=[
                ParameterSpec(
                    name="command",
                    type="text",
                    description="CLI subcommand to run. Allowed values: start, stop",
                )
            ],
        ),
    )

    assert resolver.calls == [[["command"]]]
    assert result.fingerprint.inputs[0].name == "command"
    assert normalizer.io_name_vocabulary.lookup("command") == "command"


@pytest.mark.asyncio
async def test_normalized_calendar_memo_input_enables_expected_candidates(tmp_path):
    resolver = _CreateNewIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)
    calendar_result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "calendar-memo"),
        ExtractedSkillSchema(
            inputs=[
                ParameterSpec(
                    name="command",
                    type="text",
                    description="用户指令，如 '添加 明天下午3点 团队周会'",
                )
            ],
            outputs=[ArtifactSpec(name="result", type="markdown")],
        ),
    )
    registry = SkillRegistry(
        skills={
            calendar_result.fingerprint.id: calendar_result.fingerprint,
            "speech-to-text": Fingerprint(
                type="skill",
                id="speech-to-text",
                name="speech-to-text",
                description="Transcribe audio to text.",
                version="1.0.0",
                inputs=[ParameterSpec(name="audio", type="audio")],
                outputs=[ArtifactSpec(name="text", type="text")],
            ),
            "general-writing": Fingerprint(
                type="skill",
                id="general-writing",
                name="general-writing",
                description="Write markdown content.",
                version="1.0.0",
                inputs=[ParameterSpec(name="text", type="text")],
                outputs=[ArtifactSpec(name="writing_output", type="markdown")],
            ),
        }
    )

    candidate_keys = {
        candidate.key
        for candidate in CandidateGenerator().generate(registry)
    }

    assert "speech-to-text->calendar-memo" in candidate_keys
    assert "general-writing->calendar-memo" in candidate_keys


@pytest.mark.asyncio
async def test_normalizer_aliases_same_skill_candidate_to_batch_created_term(tmp_path):
    resolver = _MappingIONameResolver(
        {
            "content": IONameResolution(
                action="create_new",
                normalized_value="content",
                confidence=0.9,
                reason="canonical",
            ),
            "text": IONameResolution(
                action="alias_existing",
                normalized_value="content",
                confidence=0.9,
                reason="same content role",
            ),
        }
    )
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "skill"),
        ExtractedSkillSchema(
            inputs=[
                ParameterSpec(name="text", type="text"),
                ParameterSpec(name="content", type="text"),
            ],
        ),
    )

    assert resolver.calls == [[["text", "content"]]]
    assert [item.name for item in result.fingerprint.inputs] == ["content"]
    assert normalizer.io_name_vocabulary.term_names() == ["content"]
    assert normalizer.io_name_vocabulary.lookup("text") == "content"
    name_methods = {
        decision.token: decision.method
        for decision in result.decisions
        if decision.field == "name" and decision.token in {"text", "content"}
    }
    assert name_methods == {
        "text": "alias_existing",
        "content": "create_new",
    }


@pytest.mark.asyncio
async def test_normalizer_aliases_cross_skill_candidate_to_batch_created_term(tmp_path):
    resolver = _MappingIONameResolver(
        {
            "content": IONameResolution(
                action="create_new",
                normalized_value="content",
                confidence=0.9,
                reason="canonical",
            ),
            "body": IONameResolution(
                action="merge_existing",
                normalized_value="content",
                confidence=0.9,
                reason="same content role",
            ),
        },
        batch_size=2,
    )
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)
    items = [
        (
            _raw_manifest(tmp_path, "skill-1"),
            ExtractedSkillSchema(outputs=[ArtifactSpec(name="content", type="text")]),
        ),
        (
            _raw_manifest(tmp_path, "skill-2"),
            ExtractedSkillSchema(outputs=[ArtifactSpec(name="body", type="text")]),
        ),
    ]

    results = await normalizer.normalize(items)

    assert resolver.calls == [[["content"], ["body"]]]
    assert [result.fingerprint.outputs[0].name for result in results] == [
        "content",
        "content",
    ]
    assert normalizer.io_name_vocabulary.term_names() == ["content"]
    assert normalizer.io_name_vocabulary.lookup("body") == "content"


@pytest.mark.asyncio
async def test_normalizer_aliases_candidate_to_existing_vocab_term(tmp_path):
    resolver = _MappingIONameResolver(
        {
            "text": IONameResolution(
                action="alias_existing",
                normalized_value="content",
                confidence=0.9,
                reason="existing synonym",
            ),
        }
    )
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)
    normalizer.io_name_vocabulary.create_term("content")

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "skill"),
        ExtractedSkillSchema(inputs=[ParameterSpec(name="text", type="text")]),
    )

    assert resolver.calls == [[["text"]]]
    assert result.fingerprint.inputs[0].name == "content"
    assert normalizer.io_name_vocabulary.lookup("text") == "content"


@pytest.mark.asyncio
async def test_normalizer_alias_to_unknown_non_batch_target_falls_back_to_token(tmp_path):
    resolver = _MappingIONameResolver(
        {
            "text": IONameResolution(
                action="alias_existing",
                normalized_value="missing_content",
                confidence=0.9,
                reason="bad target",
            ),
        }
    )
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "skill"),
        ExtractedSkillSchema(inputs=[ParameterSpec(name="text", type="text")]),
    )

    assert resolver.calls == [[["text"]]]
    assert result.fingerprint.inputs[0].name == "text"
    assert normalizer.io_name_vocabulary.term_names() == ["text"]
    assert normalizer.io_name_vocabulary.lookup("missing_content") is None


@pytest.mark.asyncio
async def test_normalizer_normalize_single_wraps_single_candidate_batch(tmp_path):
    resolver = _CreateNewIONameResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "skill"),
        ExtractedSkillSchema(outputs=[ArtifactSpec(name="late_result", type="text")]),
    )

    assert result.fingerprint.outputs[0].name == "late_result"
    assert resolver.calls == [[["late_result"]]]


@pytest.mark.asyncio
async def test_normalizer_can_exclude_candidate_from_io_name_vocab(tmp_path):
    normalizer = SkillFingerprintNormalizer(
        io_name_resolver=_ExcludeFromVocabIONameResolver(),
    )

    result = await normalizer.normalize_single(
        _raw_manifest(tmp_path, "skill"),
        ExtractedSkillSchema(
            outputs=[ArtifactSpec(name="tracking_original_copy", type="text")]
        ),
    )

    assert result.fingerprint.outputs == []
    assert normalizer.io_name_vocabulary.lookup("tracking_original_copy") is None
    assert result.decisions[-1].method == "exclude_from_vocab"


@pytest.mark.asyncio
async def test_llm_io_name_resolver_disables_thinking_and_uses_compact_vocab(monkeypatch):
    client = _CaptureJSONClient()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.fingerprint.normalize.io_name_resolver.create_llm_client",
        lambda config: client,
    )
    vocabulary = IONameVocabulary(
        version="test-vocab",
        max_vocab_size=None,
    )
    vocabulary.create_term(
        "content",
        alias="body",
        example="Long example text that should not be sent to the resolver.",
        definition="Generated written content",
    )
    resolver = LLMIONameResolver(
        LLMConfig(
            model="model-a",
            model_client_config={
                "api_key": "key",
                "api_base": "https://example.test/v1",
                "client_provider": "openai",
            },
        )
    )

    result = await resolver.resolve_async(
        [
            [
                IONameCandidate(
                    raw_value="text",
                    token="text",
                    description="Text to process",
                    direction="input",
                    data_type="text",
                    skill_id="skill-a",
                )
            ]
        ],
        vocabulary,
    )

    assert result["text"].normalized_value == "content"
    call = client.calls[0]
    assert call["request_overrides"] == _expected_thinking_disabled_overrides()
    payload = json.loads(call["user_content"])
    assert "rules" not in payload
    assert payload["vocabulary"] == [
        {
            "name": "content",
            "definition": "Generated written content",
            "aliases": ["body"],
        }
    ]
    assert "examples" not in payload["vocabulary"][0]
    assert "count" not in payload["vocabulary"][0]


@pytest.mark.asyncio
async def test_fingerprint_extractor_extract_from_root_reuses_unchanged_fingerprints(tmp_path):
    skills_root = tmp_path / "skills"
    output_dir = tmp_path / "score"
    for index in range(2):
        skill_dir = skills_root / f"skill-{index + 1}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: Skill {index + 1}\n---\n\nTest skill {index + 1}.",
            encoding="utf-8",
        )
    first_schema_extractor = _SchemaExtractor()
    first = await FingerprintExtractor(
        schema_extractor=first_schema_extractor,
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(skills_root, output_dir=output_dir)
    write_extraction_result(first, output_dir)
    _write_state_from_extraction_result(first, output_dir)

    second_schema_extractor = _SchemaExtractor()
    second = await FingerprintExtractor(
        schema_extractor=second_schema_extractor,
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(skills_root, output_dir=output_dir)
    forced_schema_extractor = _SchemaExtractor()
    forced = await FingerprintExtractor(
        schema_extractor=forced_schema_extractor,
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(skills_root, output_dir=output_dir, force=True)

    assert first.extracted_count == 2
    assert first.reused_count == 0
    assert second.extracted_count == 0
    assert second.reused_count == 2
    assert second_schema_extractor.extract_count == 0
    assert forced.extracted_count == 2
    assert forced.reused_count == 0
    assert forced_schema_extractor.extract_count == 2


@pytest.mark.asyncio
async def test_fingerprint_extractor_extract_from_root_reextracts_missing_old_fingerprint(tmp_path):
    skills_root = tmp_path / "skills"
    output_dir = tmp_path / "score"
    skill_dir = skills_root / "skill-1"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Skill 1\n---\n\nTest skill 1.",
        encoding="utf-8",
    )
    folders, current_hashes = SkillFolderScanner().snapshot(skills_root)
    write_score_state(
        ScoreState(
            skills={
                folders[0].relative_path: ScoreStateEntry(
                    skill_id="missing-skill",
                    relative_path=folders[0].relative_path,
                    skill_md_sha256=current_hashes[folders[0].relative_path],
                    fingerprint_hash="missing",
                    status="active",
                    updated_at="2026-06-06T00:00:00+00:00",
                )
            }
        ),
        output_dir,
    )
    schema_extractor = _SchemaExtractor()

    result = await FingerprintExtractor(
        schema_extractor=schema_extractor,
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(skills_root, output_dir=output_dir)

    assert result.extracted_count == 1
    assert result.reused_count == 0
    assert schema_extractor.extract_count == 1


@pytest.mark.asyncio
async def test_fingerprint_extractor_extract_from_root_reports_removed_paths_and_max_depth(tmp_path):
    skills_root = tmp_path / "skills"
    output_dir = tmp_path / "score"
    shallow_skill = skills_root / "skill-1"
    nested_skill = skills_root / "nested" / "skill-2"
    shallow_skill.mkdir(parents=True)
    nested_skill.mkdir(parents=True)
    (shallow_skill / "SKILL.md").write_text(
        "---\nname: Skill 1\n---\n\nShallow skill.",
        encoding="utf-8",
    )
    (nested_skill / "SKILL.md").write_text(
        "---\nname: Skill 2\n---\n\nNested skill.",
        encoding="utf-8",
    )
    write_score_state(
        ScoreState(
            skills={
                "removed-skill": ScoreStateEntry(
                    skill_id="removed-skill",
                    relative_path="removed-skill",
                    skill_md_sha256="old-hash",
                    fingerprint_hash="old-fingerprint-hash",
                    status="active",
                    updated_at="2026-06-06T00:00:00+00:00",
                )
            }
        ),
        output_dir,
    )

    result = await FingerprintExtractor(
        schema_extractor=_SchemaExtractor(),
        normalizer=SkillFingerprintNormalizer(
            io_name_resolver=_CreateNewIONameResolver(),
        ),
    ).extract_from_root(skills_root, output_dir=output_dir, max_depth=1)

    assert [folder.relative_path for folder in result.folders] == ["skill-1"]
    assert result.extracted_count == 1
    assert result.removed_paths == {"removed-skill"}


@pytest.mark.asyncio
async def test_graph_builder_call_emits_progress_and_supports_no_progress():
    fingerprints = [
        Fingerprint(
            type="skill",
            id="skill-1",
            name="Skill 1",
            description="Test skill",
            version="1.0.0",
            inputs=[ParameterSpec(name="input", type="text")],
            outputs=[ArtifactSpec(name="result", type="text")],
        )
    ]
    progress = []
    builder = GraphBuilder(matcher=_NoopMatcher())

    result = await builder.build(
        fingerprints,
        progress=lambda stage, **details: progress.append(
            {"stage": stage, **details}
        ),
    )
    result_without_progress = await GraphBuilder(matcher=_NoopMatcher()).build(fingerprints)

    assert result.graph.nodes
    assert result_without_progress.graph.nodes
    assert [entry["stage"] for entry in progress] == [
        "graph.registry.start",
        "graph.registry.done",
        "graph.candidates.start",
        "graph.candidates.done",
        "graph.resolve.start",
        "graph.resolve.done",
        "graph.materialize.start",
        "graph.materialize.done",
        "graph.score.start",
        "graph.score.done",
    ]


@pytest.mark.asyncio
async def test_graph_matcher_merges_concurrent_batches_by_batch_index():
    matcher = _OutOfOrderBatchMatcher()
    candidates = [
        RelationCandidate(
            source_id=f"source-{index}",
            target_id=f"target-{index}",
            relation_hints=["can_feed"],
            candidate_methods=["test"],
            priority="medium",
        )
        for index in range(1, 4)
    ]

    matches = await matcher.match(SkillRegistry(skills={}), candidates)

    assert [match.candidate_id for match in matches] == [
        "candidate-1",
        "candidate-2",
        "candidate-3",
    ]
    assert [diagnostic.message for diagnostic in matcher.diagnostics] == [
        "batch 1",
        "batch 2",
        "batch 3",
    ]


def test_default_llm_factories_keep_batch_settings_out_of_llm_config():
    runtime_config = symphony_config_from_dict(
        {
            "fingerprint": {
                "extraction": {
                    "batch_size": 3,
                    "body_limit": 5000,
                },
                "normalization": {"batch_size": 6},
            },
        }
    )
    llm_config = LLMConfig(
        model="model",
        model_client_config={
            "api_key": "key",
            "api_base": "https://example.test/v1",
            "client_provider": "openai",
        },
    )

    factory = ScoreBuildRuntimeFactory()
    extractor = factory.schema_extractor(
        llm_config,
        runtime_config.fingerprint.extraction,
    )
    resolver = factory.io_name_resolver(
        llm_config,
        runtime_config.fingerprint.normalization,
    )

    assert not hasattr(llm_config, "batch_size")
    assert extractor.batch_size == 3
    assert extractor.body_limit == 5000
    assert resolver.batch_size == 6


def test_default_io_name_resolver_requires_llm_config():
    runtime_config = symphony_config_from_dict({})

    with pytest.raises(ValueError, match="llm_config is required"):
        ScoreBuildRuntimeFactory().io_name_resolver(
            None,
            runtime_config.fingerprint.normalization,
        )


def test_skill_fingerprint_normalizer_requires_io_name_resolver():
    with pytest.raises(ValueError, match="requires io_name_resolver"):
        SkillFingerprintNormalizer()


def test_fingerprint_extractor_requires_normalizer():
    with pytest.raises(ValueError, match="requires normalizer"):
        FingerprintExtractor(schema_extractor=_SchemaExtractor())
