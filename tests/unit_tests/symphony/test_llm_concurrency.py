import asyncio
import json

import pytest

from jiuwenswarm.symphony.fingerprint.batching import gather_limited
from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ExtractedSkillSchema,
    Fingerprint,
    ParameterSpec,
    RawSkillManifest,
    SkillFolder,
)
from jiuwenswarm.symphony.fingerprint.normalize import (
    IONameResolution,
    SkillFingerprintNormalizer,
)
from jiuwenswarm.symphony.fingerprint.pipeline import FingerprintExtractor
from jiuwenswarm.symphony.graph import (
    OpenAICompatibleOntologyMatcher,
    RelationCandidate,
    SkillRegistry,
)
from jiuwenswarm.symphony.llm import LLMConfig


class _DelayedMatchClient:
    def __init__(self, delay=0.02):
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.call_count = 0

    async def complete_json_async(self, **kwargs):
        del kwargs
        self.call_count += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return json.dumps(
            {
                "matches": [
                    {
                        "id": "c1",
                        "direction": "forward",
                        "confidence": 0.95,
                    }
                ]
            }
        )


class _FailingMatchClient:
    def __init__(self):
        self.call_count = 0
        self.cancelled_count = 0

    async def complete_json_async(self, **kwargs):
        del kwargs
        self.call_count += 1
        if self.call_count == 1:
            await asyncio.sleep(0)
            raise RuntimeError("forward failed")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled_count += 1
            raise
        return '{"matches":[]}'


class _VocabularyObservingResolver:
    batch_size = 1

    def __init__(self):
        self.vocabulary_snapshots = []

    async def resolve_async(self, candidates_by_skill, vocabulary):
        self.vocabulary_snapshots.append(vocabulary.term_names())
        return {
            candidate.token: IONameResolution(
                action="create_new",
                normalized_value=candidate.token,
                confidence=0.9,
            )
            for candidates in candidates_by_skill
            for candidate in candidates
        }


def _llm_config():
    return LLMConfig(
        model="model-a",
        model_client_config={
            "api_key": "key",
            "api_base": "https://example.test/v1",
            "client_provider": "openai",
        },
    )


def _fingerprint(skill_id, *, consumes):
    return Fingerprint(
        type="skill",
        id=skill_id,
        name=skill_id,
        description="Consumes input" if consumes else "Produces result",
        version="1.0.0",
        inputs=[ParameterSpec(name="input", type="text")] if consumes else [],
        outputs=[] if consumes else [ArtifactSpec(name="result", type="text")],
    )


def _registry_and_candidates(count):
    skills = {}
    candidates = []
    for index in range(count):
        source_id = f"source-{index}"
        target_id = f"target-{index}"
        skills[source_id] = _fingerprint(source_id, consumes=False)
        skills[target_id] = _fingerprint(target_id, consumes=True)
        candidates.append(
            RelationCandidate(
                source_id=source_id,
                target_id=target_id,
                relation_hints=["can_feed"],
                candidate_methods=["test"],
                priority="high",
                evidence={
                    "directions": {
                        f"{source_id}->{target_id}": {
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
        )
    return SkillRegistry(skills=skills), candidates


def _matcher(monkeypatch, client, *, workers):
    monkeypatch.setattr(
        "jiuwenswarm.symphony.graph.matcher.openai.create_llm_client",
        lambda config: client,
    )
    return OpenAICompatibleOntologyMatcher(
        _llm_config(),
        batch_size=1,
        max_workers=workers,
        require_consensus=True,
    )


@pytest.mark.parametrize(
    ("workers", "candidate_count", "expected_max_active"),
    [(1, 1, 1), (2, 1, 2), (3, 3, 3)],
)
@pytest.mark.asyncio
async def test_graph_consensus_respects_request_worker_limit(
    monkeypatch,
    workers,
    candidate_count,
    expected_max_active,
):
    client = _DelayedMatchClient(delay=0.01)
    matcher = _matcher(monkeypatch, client, workers=workers)
    registry, candidates = _registry_and_candidates(candidate_count)

    await matcher.match(registry, candidates)

    assert client.call_count == candidate_count * 2
    assert client.max_active == expected_max_active


@pytest.mark.parametrize(
    ("candidate_count", "workers", "expected_calls", "expected_cancelled"),
    [(1, 2, 2, 1), (2, 4, 4, 3)],
)
@pytest.mark.asyncio
async def test_graph_failure_cancels_remaining_requests(
    monkeypatch,
    candidate_count,
    workers,
    expected_calls,
    expected_cancelled,
):
    client = _FailingMatchClient()
    matcher = _matcher(monkeypatch, client, workers=workers)
    registry, candidates = _registry_and_candidates(candidate_count)

    with pytest.raises(RuntimeError, match="forward failed"):
        await matcher.match(registry, candidates)

    assert client.call_count == expected_calls
    assert client.cancelled_count == expected_cancelled


@pytest.mark.asyncio
async def test_extraction_batches_preserve_order_and_worker_limit():
    active = 0
    max_active = 0

    async def run_batch(batch):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01 * (4 - batch[0]))
            return batch[0]
        finally:
            active -= 1

    results = await gather_limited(
        [[1], [2], [3]],
        max_workers=2,
        run_batch=run_batch,
    )

    assert results == [1, 2, 3]
    assert max_active == 2


@pytest.mark.asyncio
async def test_extraction_batch_failure_is_propagated():
    async def run_batch(batch):
        if batch == [2]:
            raise RuntimeError("extract failed")
        await asyncio.sleep(0)
        return batch[0]

    with pytest.raises(RuntimeError, match="extract failed"):
        await gather_limited(
            [[1], [2], [3]],
            max_workers=2,
            run_batch=run_batch,
        )


@pytest.mark.asyncio
async def test_normalization_batches_commit_vocabulary_in_order(tmp_path):
    resolver = _VocabularyObservingResolver()
    normalizer = SkillFingerprintNormalizer(io_name_resolver=resolver)
    extractor = FingerprintExtractor(
        schema_extractor=object(),
        normalizer=normalizer,
        normalization_workers=4,
        normalization_batch_size=1,
    )
    manifests = {}
    schemas = {}
    indexed_folders = []
    for index, input_name in enumerate(("first_value", "second_value")):
        folder = SkillFolder(
            id_hint=f"skill-{index}",
            path=tmp_path / f"skill-{index}",
            entry=tmp_path / f"skill-{index}" / "SKILL.md",
            relative_path=f"skill-{index}",
        )
        manifests[index] = RawSkillManifest(
            folder=folder,
            frontmatter={"name": f"skill-{index}"},
            body="Test.",
            body_sha256=f"sha-{index}",
        )
        schemas[index] = ExtractedSkillSchema(
            description="Test schema",
            inputs=[ParameterSpec(name=input_name, type="text")],
            outputs=[],
        )
        indexed_folders.append((index, folder))

    results = await extractor._normalize_schemas(
        indexed_folders,
        len(indexed_folders),
        manifests,
        schemas,
    )

    assert list(results) == [0, 1]
    assert resolver.vocabulary_snapshots == [[], ["first_value"]]
