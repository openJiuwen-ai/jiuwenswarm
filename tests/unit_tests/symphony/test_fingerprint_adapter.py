import json
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError
from openai.types.chat import ChatCompletion

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.llm.request_context import (
    is_disabled_thinking_fallback_allowed,
)
from openjiuwen.symphony import (
    FINGERPRINT_ARTIFACT_FILENAME,
    FingerprintService,
    SkillFolderScanner,
    SymphonyRuntime,
)

from jiuwenswarm.symphony.adapter import (
    FingerprintArtifactCapabilityProvider,
    FingerprintLLMAdapter,
    ScanResultCapabilityProvider,
    fingerprint_settings_from_swarm,
    graph_build_orchestration_config_from_swarm,
    graph_config_from_swarm,
)
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.build import build_graph, graph_status
from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir
from jiuwenswarm.symphony.llm import LLMConfig, thinking_disabled_request_overrides
from jiuwenswarm.symphony.service import SwarmSymphonyService


@pytest.mark.asyncio
async def test_core_fingerprint_service_builds_from_swarm_scan_adapter(tmp_path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "writer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Writer
description: Write a markdown document.
inputs:
  - name: topic
    type: string
outputs:
  - name: document
    type: markdown
---
Write a concise document for the supplied topic.
""",
        encoding="utf-8",
    )
    (skill_dir / "template.md").write_text("# {{ topic }}\n", encoding="utf-8")
    scan_result = SkillFolderScanner(skills_root).scan()
    config = symphony_config_from_dict(
        {
            "fingerprint": {
                "extraction": {
                    "workers": 2,
                    "batch_size": 3,
                }
            }
        }
    )
    artifact_root = tmp_path / "artifacts"

    artifact = await FingerprintService(
        ScanResultCapabilityProvider(scan_result),
        artifact_root,
        settings=fingerprint_settings_from_swarm(config, None),
    ).build()

    assert [item.capability_id for item in artifact.fingerprints] == ["writer"]
    assert (
        artifact.fingerprints[0].content_hash
        == scan_result.capabilities[0].content_hash
    )
    assert artifact.fingerprints[0].quality is not None
    artifact_path = artifact_root / FINGERPRINT_ARTIFACT_FILENAME
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["fingerprints"][0]["capability_id"] == "writer"
    assert "id" not in payload["fingerprints"][0]
    assert not (artifact_root / "fingerprints.json").exists()

    provider = FingerprintArtifactCapabilityProvider(artifact)
    provider_snapshot, provider_fingerprints = await provider.inventory_snapshot()
    assert provider_snapshot == artifact.source_snapshot
    assert provider_fingerprints == artifact.fingerprints


def test_swarm_settings_map_only_supported_core_controls(tmp_path):
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(tmp_path / "graph"),
            },
            "fingerprint": {
                "extraction": {
                    "workers": 7,
                    "batch_size": 5,
                    "body_limit": 1234,
                }
            },
        }
    )

    settings = fingerprint_settings_from_swarm(config, None)

    assert settings.enable_llm_extraction is False
    assert settings.enable_llm_evaluation is False
    assert settings.max_concurrency == 7
    assert settings.batch_size == 5
    assert settings.body_limit == 1234


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
async def test_fingerprint_adapter_removes_symphony_completion_token_override(
    monkeypatch,
    finish_reason,
):
    model = _CapturingFingerprintModel(finish_reason=finish_reason)
    captured_configs = []
    observed_responses = []
    monkeypatch.setattr(
        "jiuwenswarm.symphony.adapter.model_from_config",
        lambda config: captured_configs.append(config) or model,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.adapter.model_response_observer_from_config",
        lambda _config: (
            lambda response, _stage, _operation: observed_responses.append(
                (response, is_disabled_thinking_fallback_allowed())
            )
        ),
    )
    config = LLMConfig(
        model="thinking-model",
        model_config_obj={"max_tokens": 99, "extra_body": {"custom": True}},
    )

    adapter = FingerprintLLMAdapter(config)
    if finish_reason == "length":
        with pytest.raises(BaseError, match="increase max_tokens"):
            await adapter.invoke([], max_tokens=2048, timeout=30)
        response = observed_responses[0][0]
    else:
        response = await adapter.invoke([], max_tokens=2048, timeout=30)

    assert captured_configs[0].model_config_obj == {
        "max_tokens": 99,
        "extra_body": {"custom": True},
    }
    assert model.calls == [{"timeout": 30}]
    assert observed_responses == [(response, False)]
    settings = fingerprint_settings_from_swarm(
        symphony_config_from_dict({}),
        config,
    )
    assert adapter.cache_signature == settings.configuration_signature


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "truncated_content",
    [None, "", '{"description": "Partial'],
    ids=["complete", "reasoning-only", "truncated-json"],
)
async def test_swarm_graph_build_consumes_canonical_core_artifact(
    monkeypatch,
    tmp_path,
    truncated_content,
):
    skills_root = tmp_path / "skills"
    for name, input_name, output_name in (
        ("summarizer", "source_text", "summary"),
        ("translator", "summary", "translated_text"),
    ):
        skill_dir = skills_root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Produce {output_name}.\n"
            f"inputs:\n  - name: {input_name}\n    type: string\n"
            f"outputs:\n  - name: {output_name}\n    type: string\n"
            f"---\nProcess the supplied {input_name}.\n",
            encoding="utf-8",
        )
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(skills_root),
                "graph_dir": str(graph_dir),
            }
        }
    )
    model = _FingerprintAndGraphModel()
    rejected_phases = []
    fingerprint_requests = []

    async def create(_client, **kwargs):
        messages = kwargs["messages"]
        phase = "probe" if messages[-1]["content"] == "Hi" else (
            "fingerprint" if "Extract a capability fingerprint" in messages[0]["content"]
            else "matching"
        )
        if phase == "fingerprint":
            fingerprint_requests.append(kwargs)
        body = kwargs.get("extra_body") or {}
        if body.get("thinking", {}).get("type") == "disabled":
            rejected_phases.append(phase)
            raise BadRequestError(
                "该模型始终思考，不支持关闭思考；请使用 low、high 或 max。",
                response=httpx.Response(400, request=httpx.Request("POST", "https://model.test/v1")),
                body=None,
            )
        response = await model.invoke(messages)
        finish_reason = "stop"
        if phase == "fingerprint" and truncated_content is not None:
            response.content = truncated_content
            finish_reason = "length"
        return ChatCompletion(
            id="test", created=0, model="offline-test", object="chat.completion",
            choices=[{
                "index": 0, "finish_reason": finish_reason,
                "message": {
                    "role": "assistant", "content": response.content,
                    "reasoning_content": "Extracting skill inputs and outputs.",
                },
            }],
        )

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create", create,
    )
    llm_config = LLMConfig(
        model="offline-test",
        model_client_config={
            "client_provider": "openai", "api_key": "key",
            "api_base": "https://model.test/v1",
        },
        model_config_obj=thinking_disabled_request_overrides(),
    )
    llm_config.model_config_obj["max_tokens"] = 99
    monkeypatch.setattr(LLMConfig, "from_default_model", lambda: llm_config)
    monkeypatch.setattr("jiuwenswarm.symphony.service.load_symphony_config", lambda: config)

    first = await SwarmSymphonyService().refresh_graph()
    assert all(request["max_tokens"] == 99 for request in fingerprint_requests)
    assert fingerprint_requests[0]["extra_body"]["thinking"]["type"] == "disabled"
    assert "thinking" not in fingerprint_requests[1].get("extra_body", {})
    if truncated_content is not None:
        assert first["success"] is False
        assert "increase max_tokens" in first["detail"]
        assert first["build_progress"]["status"] == "error"
        assert first["build_log"][-1]["stage"] == "update.failed"
        assert rejected_phases == ["probe", "fingerprint"]
        assert not (graph_dir / ".fingerprint-cache.json").exists()
        assert not (graph_dir / FINGERPRINT_ARTIFACT_FILENAME).exists()
        assert not (graph_dir / "current.json").exists()
        assert is_disabled_thinking_fallback_allowed() is False
        return
    assert first["success"] is True, first
    second = await build_graph(
        skills_root,
        graph_dir,
        llm_config=llm_config,
        symphony_config=config,
    )

    version_dir = resolve_graph_artifact_dir(graph_dir)
    graph_payload = json.loads((version_dir / "graph.json").read_text(encoding="utf-8"))
    assert first["extracted_count"] == 2
    assert second.reused_count == 2
    assert rejected_phases == ["probe", "fingerprint", "matching"]
    assert any(
        edge["source"] == "capability:summarizer"
        and edge["target"] == "capability:translator"
        for edge in graph_payload["edges"]
    )
    assert (graph_dir / FINGERPRINT_ARTIFACT_FILENAME).is_file()
    assert (version_dir / FINGERPRINT_ARTIFACT_FILENAME).is_file()
    assert not (version_dir / "fingerprints.json").exists()
    assert graph_payload["provider_source_snapshot"]["snapshot_id"]
    assert (
        graph_payload["provider_source_snapshot"]["snapshot_id"]
        == json.loads(
            (version_dir / FINGERPRINT_ARTIFACT_FILENAME).read_text(encoding="utf-8")
        )["source_snapshot"]["snapshot_id"]
    )
    assert (
        graph_status(
            skills_root,
            graph_dir,
            llm_config=llm_config,
            symphony_config=config,
        ).stale
        is False
    )


@pytest.mark.asyncio
async def test_swarm_full_build_artifact_is_immediately_mutation_ready(
    monkeypatch,
    tmp_path,
):
    skills_root = tmp_path / "skills"
    summarizer_dir = skills_root / "summarizer"
    summarizer_dir.mkdir(parents=True)
    (summarizer_dir / "SKILL.md").write_text(
        """---
name: Summarizer
description: Summarize supplied text.
inputs:
  - name: source_text
    type: string
outputs:
  - name: summary
    type: string
---
Summarize the source faithfully.
""",
        encoding="utf-8",
    )
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(skills_root),
                "graph_dir": str(graph_dir),
            }
        }
    )
    model = _FingerprintAndGraphModel()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.adapter.model_from_config",
        lambda _config: model,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.model_from_config",
        lambda _config: model,
    )
    llm_config = LLMConfig(model="offline-test")

    baseline_build = await build_graph(
        skills_root,
        graph_dir,
        llm_config=llm_config,
        symphony_config=config,
    )
    baseline = json.loads(
        (resolve_graph_artifact_dir(graph_dir) / "graph.json").read_text(
            encoding="utf-8"
        )
    )

    translator_dir = skills_root / "translator"
    translator_dir.mkdir()
    (translator_dir / "SKILL.md").write_text(
        """---
name: Translator
description: Translate supplied text.
inputs:
  - name: source_text
    type: string
outputs:
  - name: translated_text
    type: string
---
Translate the source faithfully.
""",
        encoding="utf-8",
    )
    target_scan = SkillFolderScanner(skills_root).scan()
    target_artifact = await FingerprintService(
        ScanResultCapabilityProvider(target_scan),
        tmp_path / "target-fingerprints",
        llm=FingerprintLLMAdapter(llm_config),
        settings=fingerprint_settings_from_swarm(config, llm_config),
    ).build(force=True)
    runtime = SymphonyRuntime(
        graph_artifact_root=graph_dir,
        capability_provider=FingerprintArtifactCapabilityProvider(target_artifact),
        model=model,
        orchestration_config=graph_build_orchestration_config_from_swarm(config),
        source_snapshot=baseline["source_snapshot"],
        graph_config=graph_config_from_swarm(config),
    )

    result = await runtime.graph_engine.add_skills(
        ["translator"],
        request_id="consumer-full-build-add",
        source_revision=target_artifact.source_snapshot.snapshot_id,
    )
    published = runtime.graph_engine.read().to_dict()

    assert result.status == "published"
    assert result.previous_version == baseline_build.version
    assert {node["id"] for node in published["nodes"]} == {
        "capability:summarizer",
        "capability:translator",
    }
    assert (
        published["provider_source_snapshot"]["snapshot_id"]
        == target_artifact.source_snapshot.snapshot_id
    )


class _FingerprintAndGraphModel:
    async def invoke(self, messages, **kwargs):
        del kwargs
        if messages[-1]["content"] == "Hi":
            return SimpleNamespace(content="ok")
        system = str(messages[0].get("content") or "")
        if "Extract a capability fingerprint" in system:
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "description": "Summarize supplied text.",
                        "semantic_profile": {
                            "summary": "Summarize text.",
                            "capabilities": ["text summarization"],
                            "use_cases": ["shorten source text"],
                            "limitations": [],
                            "keywords": ["summary"],
                        },
                        "inputs": [{"name": "source_text", "type": "string"}],
                        "outputs": [{"name": "summary", "type": "string"}],
                        "classification": "writing",
                        "tags": ["summary"],
                    }
                )
            )
        payload = json.loads(messages[-1]["content"])
        return SimpleNamespace(content=json.dumps({
            "matches": [
                {
                    "id": candidate["id"],
                    "direction": next(iter(candidate["directions"])),
                    "confidence": 0.95,
                }
                for candidate in payload.get("candidates", [])
            ]
        }))


class _CapturingFingerprintModel:
    def __init__(self, *, finish_reason):
        self.finish_reason = finish_reason
        self.calls = []

    async def invoke(self, _messages, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content='{"outputs": [{"name": "result", "type": "string"}]}',
            finish_reason=self.finish_reason,
        )
