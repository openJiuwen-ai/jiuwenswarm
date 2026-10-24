from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.message import UserMessage
from openjiuwen.core.runner import Runner
from openjiuwen.core.foundation.tool.schema import ToolInfo

from jiuwenswarm.agents.harness.common.rails.eternal_conversation import background_agents
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.background_agents import (
    BackgroundAgentRunner,
    ExtractorForkContext,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation.background_specs import (
    BackgroundBuildContext,
    BackgroundEvidenceRail,
    ExtractorForkGuardRail,
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
    assert extractor.skills == ["dynamic-memory-cli"]
    assert builder.skills == ["dynamic-memory-cli"]
    assert Path(extractor.workspace.root_path).parent == Path(builder.workspace.root_path).parent
    assert extractor.sys_operation.work_config.restrict_to_sandbox is True
    assert extractor.sys_operation.work_config.sandbox_root == [str(extractor_root.resolve())]
    assert builder.sys_operation.work_config.sandbox_root == [str(builder_root.resolve())]
    assert all(rail.type != "core.subagent" for rail in extractor.rails)
    assert all(rail.type != "core.subagent" for rail in builder.rails)


def test_unified_dynamic_memory_skill_runs_builder_workflow(tmp_path: Path) -> None:
    workspace = tmp_path / "builder"
    staged_skill = workspace / "skills" / "dynamic-memory-cli"
    shutil.copytree(background_agents.VENDORED_SKILL, staged_skill)
    memory_root = workspace / "staging-memory"
    proposal_path = workspace / "proposal.json"
    batch_path = workspace / "batch.json"
    manifest_path = workspace / "runs" / "build-manifest.json"
    cli = staged_skill / "scripts" / "dynamic_memory_cli.py"
    helper = staged_skill / "scripts" / "run_builder.py"

    subprocess.run(
        [sys.executable, str(cli), "init", "--path", str(memory_root)],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    proposal_path.write_text(
        json.dumps(
            {
                "base_memory_revision": 0,
                "base_snapshot_revision": 0,
                "from_cursor": 1,
                "to_cursor": 1,
                "snapshot": {"current_state": ["Builder helper verification"]},
                "changed_uts": [
                    {
                        "action": "upsert",
                        "id": "ut-builder-helper",
                        "memory_id": "memory-builder-helper",
                        "priority": 60,
                        "content": "Builder helper verification remains searchable.",
                        "queries": ["builder helper verification"],
                        "must_include": ["Builder helper verification"],
                        "evidence_refs": ["raw-history:event-1"],
                        "source": "test-session",
                        "tags": ["verification"],
                    }
                ],
                "evidence_refs": ["raw-history:range-1-1"],
                "semantic_statement": "Preserve the Builder helper verification fact.",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            str(cli),
            "--root",
            str(memory_root),
            "publish-pending",
            "--file",
            str(proposal_path),
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(cli),
            "--root",
            str(memory_root),
            "freeze-pending",
            "--output",
            str(batch_path),
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--memory-root",
            "staging-memory",
            "--batch",
            "batch.json",
            "--output",
            "runs/build-manifest.json",
        ],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["success"] is True
    assert [item["returncode"] for item in manifest["commands"]] == [0, 0, 0]


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
    assert spec.max_iterations == 64
    assert spec.model.model_request_config == model.model_config
    assert spec.tools is not None
    assert [item.tool_info() for item in spec.tools] == [tool]
    assert all(item.stateless is False for item in spec.tools)
    assert spec.skills == []
    assert spec.sys_operation is not None
    assert spec.enable_sys_operation is True
    assert spec.sys_operation.work_config.restrict_to_sandbox is True
    assert spec.sys_operation.work_config.sandbox_root == [
        str((tmp_path / "extractor").resolve())
    ]
    assert spec.enable_security_rail is False
    assert spec.enable_tool_resilience_rail is False
    assert spec.auto_create_workspace is False
    assert any("extractor-fork-guard" in rail.type for rail in spec.rails)


def test_extractor_sys_operation_identity_changes_with_dynamic_workspace(
    tmp_path: Path,
) -> None:
    tool = ToolInfo(name="read_file", description="Read", parameters={"type": "object"})
    first, _ = build_background_deep_agent_spec(
        role="extractor",
        model=_model(),
        session_id="same-session",
        workspace_root=tmp_path / "first",
        system_prompt="same prefix",
        fork_tools=[tool],
    )
    second, _ = build_background_deep_agent_spec(
        role="extractor",
        model=_model(),
        session_id="same-session",
        workspace_root=tmp_path / "second",
        system_prompt="same prefix",
        fork_tools=[tool],
    )

    assert first.sys_operation.id != second.sys_operation.id
    assert first.sys_operation.work_config.sandbox_root == [
        str((tmp_path / "first").resolve())
    ]
    assert second.sys_operation.work_config.sandbox_root == [
        str((tmp_path / "second").resolve())
    ]


@pytest.mark.asyncio
async def test_extractor_fork_guard_denies_non_memory_worker_tools(
    tmp_path: Path,
) -> None:
    tool_info = ToolInfo(
        name="ask_user",
        description="Ask the user",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    spec, _ = build_background_deep_agent_spec(
        role="extractor",
        model=_model(),
        session_id="session-a",
        workspace_root=tmp_path / "extractor",
        system_prompt="exact foreground system",
        fork_tools=[tool_info],
    )
    assert spec.tools is not None

    registered: list[object] = []
    removed: list[str] = []

    class FakeAbilityManager:
        def list(self):
            return list(spec.tools or [])

        def add_ability(self, card, resource):
            registered.append(resource)

        def remove_ability(self, name):
            removed.append(name)

    rail = ExtractorForkGuardRail(list(spec.tools))
    rail.set_workspace(SimpleNamespace(root_path=str(tmp_path / "extractor")))
    rail.set_sys_operation(object())
    rail.init(SimpleNamespace(ability_manager=FakeAbilityManager()))

    assert len(registered) == 1
    denied = registered[0]
    assert denied.card.tool_info() == tool_info
    result = await denied.invoke({"query": "continue foreground work?"})
    assert "not available during Persist Session extraction" in result["error"]
    assert "dynamic-memory-cli" in result["instruction"]

    rail.uninit(SimpleNamespace(ability_manager=FakeAbilityManager()))
    assert removed == ["ask_user"]


@pytest.mark.asyncio
async def test_extractor_fork_spec_rebinds_read_file_to_its_sandbox(tmp_path: Path) -> None:
    workspace = tmp_path / "extractor"
    workspace.mkdir()
    (workspace / "allowed.txt").write_text("staged memory only", encoding="utf-8")
    spec, context = build_background_deep_agent_spec(
        role="extractor",
        model=_model(),
        session_id="session-a",
        workspace_root=workspace,
        system_prompt="exact foreground system",
        fork_tools=[
            ToolInfo(
                name="read_file",
                description="Read a file",
                parameters={"type": "object"},
            )
        ],
    )
    context.evidence = EvidenceWriter(tmp_path, "session-a")
    agent = spec.build(context)
    await agent.ensure_initialized()
    card = agent.ability_manager.get("read_file")
    assert card is not None
    denied = Runner.resource_mgr.get_tool(tool_id=card.id)
    assert denied is not None
    result = await denied.invoke({"file_path": "allowed.txt"})
    assert result.success is True
    assert "staged memory only" in result.data["content"]
    await agent.stop()


@pytest.mark.asyncio
async def test_extractor_proposal_write_returns_only_text_validation_feedback(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "extractor"
    spec, context = build_background_deep_agent_spec(
        role="extractor",
        model=_model(),
        session_id="session-proposal",
        workspace_root=workspace,
        system_prompt="exact foreground system",
        fork_tools=[
            ToolInfo(
                name="write_file",
                description="Write a file",
                parameters={"type": "object"},
            )
        ],
    )
    context.evidence = EvidenceWriter(tmp_path, "session-proposal")
    proposal_path = workspace / "runs" / "one" / "output" / "proposal.json"

    def validate(proposal: dict) -> None:
        content = proposal["changed_uts"][0]["content"]
        if len(content) > 700:
            raise ValueError(
                f'UT "atlas-durability-contract" content is {len(content)} '
                "characters; maximum is 700"
            )

    context.extractor_proposal_validation.configure(proposal_path, validate)
    agent = spec.build(context)
    await agent.ensure_initialized()
    card = agent.ability_manager.get("write_file")
    assert card is not None
    tool = Runner.resource_mgr.get_tool(tool_id=card.id)
    assert tool is not None

    oversized = {
        "action": "add_ut",
        "value": {
            "action": "upsert",
            "id": "atlas-durability-contract",
            "priority": 100,
            "content": "x" * 701,
            "queries": ["Atlas"],
            "must_include": ["x"],
        },
    }
    rejected = await tool.invoke(
        {
            "file_path": "runs/one/output/proposal.json",
            "content": json.dumps(oversized),
        }
    )

    assert rejected.success is False
    assert rejected.data is None
    assert rejected.error == (
        "Proposal validation failed: UT content must be a non-empty string of "
        "at most 700 characters. Correct the proposal and call write_file again."
    )
    assert "changed_uts" not in rejected.error
    assert tool.render_for_llm(rejected) == rejected.error
    assert not proposal_path.exists()

    corrected_ut = {
        "action": "upsert",
        "id": "atlas-durability-contract",
        "priority": 100,
        "content": "x" * 700,
        "queries": ["Atlas"],
        "must_include": ["x"],
    }
    accepted = await tool.invoke(
        {
            "file_path": "runs/one/output/proposal.json",
            "content": json.dumps({"action": "add_ut", "value": corrected_ut}),
        }
    )
    assert accepted.success is True
    assert "accepted" in accepted.data["content"]
    assert not proposal_path.exists()

    snapshot_item = await tool.invoke(
        {
            "file_path": "runs/one/output/proposal.json",
            "content": json.dumps(
                {
                    "action": "add_snapshot_item",
                    "section": "constraints",
                    "value": "Atlas must remain durable.",
                }
            ),
        }
    )
    assert snapshot_item.success is True

    finalized = await tool.invoke(
        {
            "file_path": "runs/one/output/proposal.json",
            "content": json.dumps(
                {"action": "finalize", "semantic_statement": "Atlas preserved."}
            ),
        }
    )
    assert finalized.success is True
    assert finalized.data["content"].endswith(
        'Return only {"status":"finalized"}; invoke no more tools.'
    )
    stored = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert stored["changed_uts"] == [corrected_ut]
    assert stored["snapshot"]["constraints"] == ["Atlas must remain durable."]
    assert stored["semantic_statement"] == "Atlas preserved."
    await agent.stop()


@pytest.mark.asyncio
async def test_extractor_fork_shell_only_runs_staged_memory_cli(tmp_path: Path) -> None:
    workspace = tmp_path / "extractor"
    staged_skill = workspace / "skills" / "dynamic-memory-cli"
    shutil.copytree(background_agents.VENDORED_SKILL, staged_skill)
    memory_root = workspace / "runs" / "one" / "memory"
    cli = staged_skill / "scripts" / "dynamic_memory_cli.py"
    subprocess.run(
        [sys.executable, str(cli), "init", "--path", str(memory_root)],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    spec, context = build_background_deep_agent_spec(
        role="extractor",
        model=_model(),
        session_id="session-shell",
        workspace_root=workspace,
        system_prompt="exact foreground system",
        fork_tools=[
            ToolInfo(
                name="bash",
                description="Run a shell command",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            )
        ],
    )
    context.evidence = EvidenceWriter(tmp_path, "session-shell")
    agent = spec.build(context)
    await agent.ensure_initialized()
    card = agent.ability_manager.get("bash")
    assert card is not None
    tool = Runner.resource_mgr.get_tool(tool_id=card.id)
    assert tool is not None

    allowed = await tool.invoke(
        {
            "command": (
                "python skills/dynamic-memory-cli/scripts/dynamic_memory_cli.py "
                "--root runs/one/memory list --full"
            )
        }
    )
    assert allowed.success is True
    assert '"memories"' in allowed.data["content"]
    blocked = await tool.invoke({"command": "python -c \"print('escape')\""})
    assert blocked.success is False
    assert "only the staged dynamic-memory-cli script" in str(blocked.error)
    escaped_output = await tool.invoke(
        {
            "command": (
                "python skills/dynamic-memory-cli/scripts/dynamic_memory_cli.py "
                "--root runs/one/memory check-conflicts --file ../outside.json"
            )
        }
    )
    assert escaped_output.success is False
    assert "outside the Extractor workspace" in str(escaped_output.error)
    assert context.extractor_tool_calls[-1]["kind"] == "dynamic-memory-cli"
    await agent.stop()


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
    staged_skill = request_path.parents[1] / "skills" / "dynamic-memory-cli"
    assert (staged_skill / "SKILL.md").is_file()
    assert (staged_skill / "references" / "builder-contract.md").is_file()
    assert (staged_skill / "references" / "extractor-contract.md").is_file()
    assert (staged_skill / "scripts" / "run_builder.py").is_file()

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
    assert "The foreground task stops here" in observed["inputs"]["query"]
    assert "active workspace" in observed["inputs"]["query"]
    assert "has changed to" in observed["inputs"]["query"]
    cache_rows = [
        json.loads(line)
        for line in (tmp_path / "audit" / "extractor-fork-cache.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert cache_rows[-1]["fork_prefix_hit_rate"] == 1.0
    assert cache_rows[-1]["full_prefix_hit"] is True


@pytest.mark.asyncio
async def test_extractor_run_renders_dynamic_paths_and_verifies_written_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BackgroundAgentRunner(
        lambda: _model(),
        EvidenceWriter(tmp_path, "session-a"),
        root=tmp_path,
        session_id="session-a",
    )
    workspace = runner.prepare_workspace("extractor")
    proposal = {
        "snapshot": {
            "resident_memory": [],
            "recent_context": [],
            "current_state": [],
            "completed": [],
            "next_actions": [],
            "constraints": [],
        },
        "changed_uts": [],
        "semantic_statement": "No durable change.",
    }
    observed: dict[str, str] = {}

    async def fake_invoke(**kwargs):
        observed["prompt"] = kwargs["prompt"]
        output = workspace / "runs" / "one" / "output" / "proposal.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(proposal), encoding="utf-8")
        return (
            "Max iterations reached without completion",
            {},
            {
                "extractor_tool_calls": [
                    {"tool": "bash", "kind": "dynamic-memory-cli"},
                    {"tool": "write_file"},
                ]
            },
        )

    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    fork = ExtractorForkContext(
        system_prompt="system",
        messages=(),
        tools=(),
        parent_session_id="session-a",
        source_input_tokens=10,
        context_window_tokens=100,
        prefix_sha256="prefix",
    )
    request = {
        "batch": 1,
        "extractor_run": {
            "memory_root": "runs/one/memory",
            "request": "runs/one/input/latest-request.json",
            "proposal": "runs/one/output/proposal.json",
        },
    }

    result = await runner.call_json(
        role="extractor",
        system_prompt="extract",
        request=request,
        fork_context=fork,
    )

    assert result == proposal
    assert str(workspace.resolve()) in observed["prompt"]
    assert "--root runs/one/memory list --full" in observed["prompt"]
    assert "runs/one/output/proposal.json" in observed["prompt"]
    assert "add_snapshot_item" in observed["prompt"]
    assert 'return only {"status":"finalized"}' in observed["prompt"]
    stored_request = workspace / "runs" / "one" / "input" / "latest-request.json"
    assert json.loads(stored_request.read_text(encoding="utf-8")) == request


@pytest.mark.asyncio
async def test_extractor_fork_accepts_prefix_over_seventy_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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

    async def fake_invoke(**kwargs):
        assert kwargs["fork_context"] is fork
        return (
            '{"snapshot":{"resident_memory":[],"recent_context":[],"current_state":[],'
            '"completed":[],"next_actions":[],"constraints":[]},"changed_uts":[],'
            '"semantic_statement":"over-boundary fork accepted"}',
            {},
            {},
        )

    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    result = await runner.call_json(
        role="extractor",
        system_prompt="extract",
        request={"batch": 1},
        fork_context=fork,
    )

    assert result["semantic_statement"] == "over-boundary fork accepted"
    assert fork.extraction_boundary_reached is True


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
