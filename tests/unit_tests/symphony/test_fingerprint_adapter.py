import asyncio
import json
from pathlib import Path
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

from jiuwenswarm.common.symphony_config import symphony_config_from_dict
from jiuwenswarm.runtime.symphony.build import build_graph, graph_status
from jiuwenswarm.runtime.symphony.graph_storage import resolve_graph_artifact_dir
from jiuwenswarm.runtime.symphony.llm import LLMConfig, thinking_disabled_request_overrides
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService








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
    graph_dir = tmp_path / "runtime" / "graph"
    config = symphony_config_from_dict(
        {
            "capability": {"sources": [{"type": "skill", "root": str(skills_root)}]},
            "storage": {"artifact_root": str(tmp_path / "runtime")},
            "orchestration": {"enabled": True}
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
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)

    first = await SwarmSymphonyService().refresh_graph()
    assert all(request["max_tokens"] == 99 for request in fingerprint_requests)
    assert fingerprint_requests[0]["extra_body"]["thinking"]["type"] == "disabled"
    assert "thinking" not in fingerprint_requests[1].get("extra_body", {})
    if truncated_content is not None:
        assert first["success"] is False
        assert "finish_reason=length" in first["detail"]
        assert first["build_progress"]["status"] == "error"
        assert first["build_log"][-1]["stage"] == "update.failed"
        assert rejected_phases == ["probe", "fingerprint"]
        assert not (graph_dir / ".fingerprint-cache.json").exists()
        assert not (graph_dir / FINGERPRINT_ARTIFACT_FILENAME).exists()
        assert not (graph_dir / "current.json").exists()
        assert is_disabled_thinking_fallback_allowed() is False
        return
    assert first["success"] is True, first
    second = await build_graph(config, llm_config)

    version_dir = resolve_graph_artifact_dir(graph_dir)
    graph_payload = json.loads((version_dir / "graph.json").read_text(encoding="utf-8"))
    assert first["extracted_count"] == 2
    assert second.reused_count == 2
    assert rejected_phases == ["probe", "fingerprint"]
    assert any(
        edge["source"] == "capability:summarizer"
        and edge["target"] == "capability:translator"
        for edge in graph_payload["edges"]
    )
    assert (tmp_path / "runtime" / "cache" / "fingerprints" / FINGERPRINT_ARTIFACT_FILENAME).is_file()
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
        graph_status(config, llm_config=llm_config).stale
        is False
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


def write_capability(root, name, body="Summarize supplied text."):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Summarize text.\n---\n{body}")
    return path


def runtime_config(tmp_path, **overrides):
    return symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "orchestration": {"enabled": True},
        **overrides,
    })


@pytest.mark.asyncio
async def test_runtime_applies_fingerprint_concurrency_and_request_policy(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    for index in range(5):
        write_capability(root, f"capability-{index}", "a" * 200 + "TAIL_SHOULD_BE_TRUNCATED")
    class Model(_FingerprintAndGraphModel):
        active = 0
        peak = 0
        requests = []
        async def invoke(self, messages, **kwargs):
            if "Extract a capability fingerprint" in messages[0]["content"]:
                self.requests.append((messages, kwargs))
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(0.01)
                    return await super().invoke(messages, **kwargs)
                finally:
                    self.active -= 1
            return await super().invoke(messages, **kwargs)
    model = Model()
    observed = []
    model.symphony_response_observer = lambda response, stage, operation: observed.append(stage)
    config = runtime_config(tmp_path,
        capability={"sources": [{"type": "skill", "root": str(root)}], "fingerprint": {"extraction": {
            "max_concurrency": 5, "batch_size": 3, "max_body_chars": 100,
            "timeout_seconds": 7, "max_output_tokens": None}}},
        orchestration={"enabled": True, "build": {"max_concurrency": 2}})
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.build.model_from_config", lambda _: model)
    events = []
    result = await build_graph(config, LLMConfig(model="test"), build_log=lambda event, **details: events.append(event))
    assert result.success and result.extracted_count == 5
    assert model.peak == 2 and model.active == 0
    assert len(model.requests) == 5
    assert all(kwargs["timeout"] == 7 and "max_tokens" not in kwargs for _, kwargs in model.requests)
    assert all("TAIL_SHOULD_BE_TRUNCATED" not in str(messages) for messages, _ in model.requests)
    assert observed.count("fingerprint_extraction") == 5
    assert "fingerprint.extract.progress" in events
    assert result.build_id and result.version


@pytest.mark.asyncio
async def test_runtime_build_and_status_own_inventory_and_incremental_fingerprints(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    first_file = write_capability(root, "writer")
    config = runtime_config(tmp_path)
    model = _FingerprintAndGraphModel()
    model.model_config = SimpleNamespace(model_name="test", temperature=0, max_tokens=2048)
    model.model_client_config = SimpleNamespace(client_provider="openai", api_base="https://model.test", api_key="fake")
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.build.model_from_config", lambda _: model)
    llm = LLMConfig(model="test")
    first = await build_graph(config, llm)
    assert first.extracted_count == 1
    assert graph_status(config).stale is False
    second_file = write_capability(root, "translator")
    pending = graph_status(config)
    assert pending.added_count == 1 and pending.stale
    second = await build_graph(config, llm)
    assert second.reused_count == 1 and second.extracted_count == 1
    assert second.version != first.version
    first_file.write_text(first_file.read_text() + "\nAdditional processing step.")
    assert graph_status(config).changed_count == 1
    second_file.unlink()
    assert graph_status(config).removed_count == 1
    changed = await build_graph(config, llm)
    assert changed.removed_count == 1 and changed.extracted_count == 1
    assert graph_status(config).stale is False
    runtime = SymphonyRuntime(None, config)
    payload = runtime.orchestration.read().to_dict()
    assert {item["capability_id"] for item in payload["capabilities"]} == {"writer"}
    await runtime.aclose()


@pytest.mark.asyncio
async def test_failed_application_artifact_preserves_published_version(tmp_path, monkeypatch):
    write_capability(tmp_path / "skills", "writer")
    config = runtime_config(tmp_path)
    model = _FingerprintAndGraphModel()
    runtime = SymphonyRuntime(model, config)
    first = await runtime.orchestration.build()
    def fail(_path):
        raise OSError("application output failed")
    with pytest.raises(OSError, match="application output"):
        await runtime.orchestration.build(force=True, prepare_artifact=fail)
    assert runtime.orchestration.status().version == first.version
    assert runtime.orchestration.read().get("build_id") == first.build_id
    await runtime.aclose()
