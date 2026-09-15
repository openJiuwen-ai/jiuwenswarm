from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message import UserMessage
from openjiuwen.core.foundation.tool.schema import ToolInfo

from jiuwenswarm.agents.harness.common.rails.eternal_conversation import background_agents
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.background_agents import (
    BackgroundAgentRunner,
    ExtractorForkContext,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.background_specs import (
    BackgroundBuildContext,
    BackgroundEvidenceRail,
    build_background_deep_agent_spec,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.evidence import (
    EvidenceWriter,
)


def _model() -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_key="test-key",
            api_base="https://example.test/v1",
            verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model="test-model"),
    )


def test_background_specs_are_independent_react_agents_with_sibling_sandboxes(
    tmp_path: Path,
) -> None:
    model = _model()
    extractor_root = tmp_path / "jobs" / "eternal" / "extractor"
    builder_root = tmp_path / "jobs" / "eternal" / "builder"
    extractor, _ = build_background_deep_agent_spec(
        role="extractor",
        model=model,
        session_id="session-a",
        workspace_root=extractor_root,
        system_prompt="extract",
    )
    builder, _ = build_background_deep_agent_spec(
        role="builder",
        model=model,
        session_id="session-a",
        workspace_root=builder_root,
        system_prompt="build",
    )

    assert extractor.card.id != builder.card.id
    assert extractor.enable_task_loop is False
    assert builder.enable_task_loop is False
    assert extractor.max_iterations == builder.max_iterations == 12
    assert extractor.skills == ["persist-session-extractor"]
    assert builder.skills == ["persist-session-builder", "dynamic-memory-cli"]
    assert Path(extractor.workspace.root_path).parent == Path(builder.workspace.root_path).parent
    assert extractor.sys_operation.work_config.restrict_to_sandbox is True
    assert extractor.sys_operation.work_config.sandbox_root == [str(extractor_root.resolve())]
    assert builder.sys_operation.work_config.sandbox_root == [str(builder_root.resolve())]
    assert all(rail.type != "core.subagent" for rail in extractor.rails)
    assert all(rail.type != "core.subagent" for rail in builder.rails)


def test_extractor_fork_spec_preserves_system_tools_and_model_request_config(
    tmp_path: Path,
) -> None:
    model = _model()
    tool = ToolInfo(
        name="read_file",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    spec, _ = build_background_deep_agent_spec(
        role="extractor",
        model=model,
        session_id="session-a",
        workspace_root=tmp_path / "extractor",
        system_prompt="byte-identical foreground system",
        fork_tools=[tool],
    )

    assert spec.system_prompt == "byte-identical foreground system"
    assert spec.model.model_request_config == model.model_config
    assert spec.tools is not None
    assert [item.tool_info() for item in spec.tools] == [tool]
    assert spec.skills == []
    assert spec.sys_operation is None
    assert spec.enable_sys_operation is False
    assert spec.enable_security_rail is False
    assert spec.enable_tool_resilience_rail is False
    assert any("extractor-fork-guard" in rail.type for rail in spec.rails)


@pytest.mark.asyncio
async def test_product_model_runner_materializes_and_invokes_deep_agent_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    class FakeDeepAgent:
        card = SimpleNamespace(id="background")

        async def invoke(self, inputs, session=None):
            calls.append({"inputs": inputs, "session": session})
            return {"output": '{"approved":true,"diagnostics":[]}'}

        async def stop(self):
            calls.append({"stopped": True})

    class FakeSpec:
        def build(self, context):
            assert isinstance(context, BackgroundBuildContext)
            assert context.evidence is not None
            return FakeDeepAgent()

    def fake_spec_builder(**kwargs):
        return FakeSpec(), BackgroundBuildContext(role=kwargs["role"])

    monkeypatch.setattr(
        background_agents,
        "build_background_deep_agent_spec",
        fake_spec_builder,
    )
    runner = BackgroundAgentRunner(
        lambda: _model(),
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )
    result = await runner.call_json(
        role="builder",
        system_prompt="build",
        request={"batch": 1},
    )

    assert result["approved"] is True
    assert calls[0]["inputs"]["conversation_id"] == "session-a:persist:builder"
    assert calls[0]["session"]._session_id.startswith(
        "session-a:persist:builder:"
    )
    request_path = tmp_path / "jobs" / "eternal" / "builder" / "input" / "latest-request.json"
    assert json.loads(request_path.read_text(encoding="utf-8")) == {"batch": 1}
    assert (request_path.parents[1] / "skills" / "persist-session-builder" / "SKILL.md").is_file()
    assert (request_path.parents[1] / "skills" / "dynamic-memory-cli" / "SKILL.md").is_file()

    await runner.close()
    assert calls[-1] == {"stopped": True}


@pytest.mark.asyncio
async def test_background_runtime_follows_selected_model_and_stops_replaced_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []

    class FakeDeepAgent:
        card = SimpleNamespace(id="background")

        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        async def invoke(self, inputs, session=None):
            events.append(("invoke", self.model_name))
            return {"output": '{"approved":true,"diagnostics":[]}'}

        async def stop(self):
            events.append(("stop", self.model_name))

    class FakeSpec:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def build(self, context):
            return FakeDeepAgent(self.model_name)

    def fake_spec_builder(**kwargs):
        model_name = kwargs["model"].model_config.model_name
        return FakeSpec(model_name), BackgroundBuildContext(role=kwargs["role"])

    monkeypatch.setattr(background_agents, "build_background_deep_agent_spec", fake_spec_builder)
    selected = _model()
    runner = BackgroundAgentRunner(
        lambda: selected,
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )
    await runner.call_json(role="builder", system_prompt="build", request={"batch": 1})

    selected = Model(
        model_client_config=selected.model_client_config,
        model_config=ModelRequestConfig(model="replacement-model"),
    )
    await runner.call_json(role="builder", system_prompt="build", request={"batch": 2})

    assert events[:3] == [
        ("invoke", "test-model"),
        ("stop", "test-model"),
        ("invoke", "replacement-model"),
    ]
    await runner.close()
    assert events[-1] == ("stop", "replacement-model")


@pytest.mark.asyncio
async def test_each_background_job_uses_a_fresh_execution_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_ids: list[str] = []

    class FakeDeepAgent:
        card = SimpleNamespace(id="background")

        async def invoke(self, inputs, session=None):
            session_ids.append(session._session_id)
            return {"output": '{"approved":true,"diagnostics":[]}'}

        async def stop(self):
            return None

    class FakeSpec:
        def build(self, context):
            return FakeDeepAgent()

    monkeypatch.setattr(
        background_agents,
        "build_background_deep_agent_spec",
        lambda **kwargs: (FakeSpec(), BackgroundBuildContext(role=kwargs["role"])),
    )
    runner = BackgroundAgentRunner(
        lambda: _model(),
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )

    await runner.call_json(role="extractor", system_prompt="extract", request={"batch": 1})
    await runner.call_json(role="extractor", system_prompt="extract", request={"batch": 2})

    assert len(session_ids) == 2
    assert session_ids[0] != session_ids[1]
    assert all(value.startswith("session-a:persist:extractor:") for value in session_ids)


@pytest.mark.asyncio
async def test_timed_out_background_runtime_is_stopped_and_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    stopped: list[object] = []

    class FakeDeepAgent:
        card = SimpleNamespace(id="background")

        async def invoke(self, inputs, session=None):
            await asyncio.Event().wait()

        async def stop(self):
            stopped.append(self)

    class FakeSpec:
        def build(self, context):
            agent = FakeDeepAgent()
            built.append(agent)
            return agent

    monkeypatch.setattr(
        background_agents,
        "build_background_deep_agent_spec",
        lambda **kwargs: (FakeSpec(), BackgroundBuildContext(role=kwargs["role"])),
    )
    monkeypatch.setattr(background_agents, "MAX_BACKGROUND_ATTEMPTS", 2)
    monkeypatch.setattr(background_agents, "BACKGROUND_INVOCATION_TIMEOUT_SECONDS", 0.01)
    runner = BackgroundAgentRunner(
        lambda: _model(),
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )

    with pytest.raises(TimeoutError, match="exceeded"):
        await runner.call_json(
            role="extractor", system_prompt="extract", request={"batch": 1}
        )

    assert len(built) == 2
    assert stopped == built


@pytest.mark.asyncio
async def test_builder_retry_uses_builder_specific_completion_checklist(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    class FakeModel:
        async def invoke(self, messages, temperature=0):
            prompts.append(messages[-1].content)
            if len(prompts) == 1:
                return SimpleNamespace(content="Max iterations reached without completion")
            return SimpleNamespace(
                content=(
                    '{"approved":true,"diagnostics":[],'
                    '"build_manifest":"runs/build-1/build-manifest.json"}'
                )
            )

    runner = BackgroundAgentRunner(
        lambda: FakeModel(),
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )

    result = await runner.call_json(
        role="builder", system_prompt="build", request={"batch": 1}
    )

    assert result["approved"] is True
    assert "skipped" in prompts[1]
    assert "inspect SQLite" in prompts[1]
    assert "Snapshot arrays" not in prompts[1]


@pytest.mark.asyncio
async def test_extractor_fork_reuses_exact_prefix_and_audits_full_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict = {}

    class FakeDeepAgent:
        card = SimpleNamespace(id="extractor-fork")

        def __init__(self, context: BackgroundBuildContext) -> None:
            self.context = context

        async def create_new_context_engine(self, session_id=None, messages=None):
            observed["context_session_id"] = session_id
            observed["messages"] = messages
            return session_id

        async def invoke(self, inputs, session=None):
            observed["inputs"] = inputs
            observed["parent_session_id"] = session.get_parent_session_id()
            self.context.model_usages.append(
                {"input_tokens": 112, "cache_tokens": 100}
            )
            self.context.fork_prefix_checks.append(
                {"actual_sha256": "prefix-hash", "matches": True}
            )
            return {"output": '{"snapshot":{"resident_memory":[],"recent_context":[],"current_state":[],"completed":[],"next_actions":[],"constraints":[]},"changed_uts":[],"semantic_statement":"ok"}'}

        async def stop(self):
            return None

    class FakeSpec:
        def build(self, context):
            observed["build_context"] = context
            return FakeDeepAgent(context)

    def fake_spec_builder(**kwargs):
        observed["spec_kwargs"] = kwargs
        return FakeSpec(), BackgroundBuildContext(role=kwargs["role"])

    monkeypatch.setattr(background_agents, "build_background_deep_agent_spec", fake_spec_builder)
    writer = EvidenceWriter(tmp_path, "session-a")
    runner = BackgroundAgentRunner(
        lambda: _model(),
        writer,
        root=tmp_path,
        session_id="session-a",
    )
    fork = ExtractorForkContext(
        system_prompt="exact foreground system",
        messages=(UserMessage(content="foreground request"),),
        tools=(
            ToolInfo(
                name="read_file",
                description="Read a file",
                parameters={"type": "object"},
            ),
        ),
        parent_session_id="session-a",
        source_input_tokens=100,
        context_window_tokens=1000,
        prefix_sha256="prefix-hash",
    )

    await runner.call_json(
        role="extractor",
        system_prompt="must not become the system prompt",
        request={"batch": 1},
        fork_context=fork,
    )

    assert observed["spec_kwargs"]["system_prompt"] == "exact foreground system"
    assert observed["spec_kwargs"]["fork_tools"][0].name == "read_file"
    assert observed["messages"][0].content == "foreground request"
    assert observed["parent_session_id"] == "session-a"
    assert "Start the Persist Session" in observed["inputs"]["query"]
    cache_rows = [
        json.loads(line)
        for line in (tmp_path / "audit" / "extractor-fork-cache.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert cache_rows[-1]["fork_prefix_hit_rate"] == 1.0
    assert cache_rows[-1]["full_prefix_hit"] is True


@pytest.mark.asyncio
async def test_extractor_fork_rejects_prefix_over_seventy_percent(
    tmp_path: Path,
) -> None:
    runner = BackgroundAgentRunner(
        lambda: _model(),
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )
    fork = ExtractorForkContext(
        system_prompt="system",
        messages=(),
        tools=(),
        parent_session_id="session-a",
        source_input_tokens=701,
        context_window_tokens=1000,
        prefix_sha256="over-budget",
    )

    with pytest.raises(ValueError, match="exceeds 70%"):
        await runner.call_json(
            role="extractor",
            system_prompt="extract",
            request={"batch": 1},
            fork_context=fork,
        )


@pytest.mark.asyncio
async def test_background_evidence_rail_verifies_exact_fork_prefix(
    tmp_path: Path,
) -> None:
    parent_messages = [
        {"role": "system", "content": "exact system"},
        {"role": "user", "content": "foreground task"},
    ]
    tools = [
        {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object"},
        }
    ]
    encoded = json.dumps(
        {"messages": parent_messages, "tools": tools},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected = hashlib.sha256(encoded).hexdigest()
    usages: list[dict] = []
    checks: list[dict] = []
    rail = BackgroundEvidenceRail(
        EvidenceWriter(tmp_path, "session-a"),
        "extractor",
        usages,
        expected,
        2,
        checks,
    )
    await rail.after_model_call(
        SimpleNamespace(
            inputs=SimpleNamespace(
                messages=[*parent_messages, {"role": "user", "content": "extract"}],
                tools=tools,
                response=SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        model_dump=lambda **_kwargs: {
                            "input_tokens": 110,
                            "cache_tokens": 100,
                        }
                    )
                ),
                react_iteration=0,
            ),
            exception=None,
        )
    )

    assert checks == [
        {
            "expected_sha256": expected,
            "actual_sha256": expected,
            "matches": True,
        }
    ]


@pytest.mark.asyncio
async def test_background_evidence_rail_restores_exact_fork_system_prompt(
    tmp_path: Path,
) -> None:
    exact = "exact Worker system prompt\n"
    identity = SimpleNamespace(content={"cn": exact.strip(), "en": exact.strip()})
    builder = SimpleNamespace(
        get_section=lambda name: identity if name == "identity" else None
    )
    rail = BackgroundEvidenceRail(
        EvidenceWriter(tmp_path, "session-a"),
        "extractor",
        [],
        None,
        0,
        [],
        exact,
    )
    rail.system_prompt_builder = builder

    await rail.before_model_call(
        SimpleNamespace(
            inputs=SimpleNamespace(messages=[], tools=[], react_iteration=1),
            exception=None,
        )
    )

    assert identity.content == {"cn": exact, "en": exact}
