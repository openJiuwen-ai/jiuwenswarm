# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.foundation.llm import (
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.sys_operation import SysOperationCard
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec, WorkspaceSpec

from jiuwenswarm.agents.harness.code.spec import (
    CODE_RAIL_BUNDLE,
    CODE_SUBAGENT_BUNDLE,
    CODE_TOOL_BUNDLE,
    convert_code_config_to_deep_agent_spec,
)
from jiuwenswarm.agents.harness.code import spec as code_spec_module
from jiuwenswarm.server.runtime.agent_adapter import interface_code


class _FakeCodeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rail_names: list[str] = []
        self.subagent_config: dict = {}

    @contextmanager
    def _code_spec_config_scope(self, _config):
        yield

    def _build_agent_rails(self, config, config_base, *, mode):
        assert config == config_base["react"]
        assert mode == "code"
        self.rail_names = list(config_base["modes"]["code"]["rails"])
        self.calls.append("rails")
        return []

    def _build_configured_subagents(self, model, config, config_base):
        assert model is not None
        assert config == config_base["react"]
        self.subagent_config = dict(config.get("subagents") or {})
        self.calls.append("subagents")
        return [], False

    def _get_tool_build_func(self, tool_name, agent_id):
        raise AssertionError(f"unexpected tool build: {tool_name}/{agent_id}")

    @staticmethod
    def _tool_owner_id():
        return "code-spec-test-agent"


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


def test_code_config_is_converted_to_spec_snapshot(tmp_path):
    adapter = _FakeCodeAdapter()
    config_base = {
        "progressive_tool_enabled": True,
        "react": {
            "enable_task_loop": True,
            "max_iterations": 23,
            "subagents": {"code_agent": {"enabled": True}},
        },
        "modes": {
            "code": {
                "rails": ["SkillUseRail"],
                "tools": ["web_free_search"],
            }
        },
    }
    sysop_card = SysOperationCard(
        id="code-spec-shape",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None),
    )
    sysop = SimpleNamespace(id=sysop_card.id)

    spec, context = convert_code_config_to_deep_agent_spec(
        adapter=adapter,
        config_base=config_base,
        react_config=config_base["react"],
        model=_model(),
        card=AgentCard(id="code-spec-agent", name="code-spec-agent"),
        system_prompt="code prompt",
        workspace_root=str(tmp_path),
        project_dir=str(tmp_path),
        sys_operation=sysop,
        sys_operation_card=sysop_card,
        language="en",
        context_engine_config={"context_window_tokens": 8192},
        kv_cache_affinity_config=None,
        enable_read_image_multimodal=False,
        completion_timeout=None,
        session_id="session-1",
        channel_id="web",
    )

    assert type(spec) is DeepAgentSpec
    assert [rail.type for rail in spec.rails] == [CODE_RAIL_BUNDLE]
    assert [tool.type for tool in spec.tools] == [CODE_TOOL_BUNDLE]
    assert [subagent.factory_name for subagent in spec.subagents] == [
        CODE_SUBAGENT_BUNDLE
    ]
    assert spec.rails[0].params == {"configured_rails": ["SkillUseRail"]}
    assert spec.tools[0].params == {"configured_tools": ["web_free_search"]}
    assert spec.subagents[0].factory_kwargs == {
        "subagents": {"code_agent": {"enabled": True}},
        "max_iterations": 23,
    }
    assert spec.enable_task_loop is True
    assert spec.progressive_tool is not None
    assert spec.progressive_tool.enabled is True
    assert spec.max_iterations == 23
    assert spec.completion_timeout is None
    assert spec.workspace.root_path == str(tmp_path)
    assert spec.sys_operation.id == sysop_card.id
    assert context.session_id == "session-1"
    assert context.channel_id == "web"
    assert context.tool_owner_id == "code-spec-test-agent"
    restored = DeepAgentSpec.model_validate_json(spec.model_dump_json())
    assert restored.model_dump(mode="json") == spec.model_dump(mode="json")


def test_code_spec_forces_task_loop_for_skill_evolution(tmp_path):
    adapter = _FakeCodeAdapter()
    config_base = {
        "progressive_tool_enabled": False,
        "react": {
            "enable_task_loop": False,
            "evolution": {"skill_evolution": True},
        },
        "modes": {"code": {"rails": [], "tools": []}},
    }
    sysop_card = SysOperationCard(
        id="code-spec-evolution",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None),
    )

    spec, _context = convert_code_config_to_deep_agent_spec(
        adapter=adapter,
        config_base=config_base,
        react_config=config_base["react"],
        model=_model(),
        card=AgentCard(id="code-spec-evolution", name="code-spec-evolution"),
        system_prompt="code prompt",
        workspace_root=str(tmp_path),
        project_dir=str(tmp_path),
        sys_operation=SimpleNamespace(id=sysop_card.id),
        sys_operation_card=sysop_card,
        language="en",
        context_engine_config=None,
        kv_cache_affinity_config=None,
        enable_read_image_multimodal=False,
        completion_timeout=None,
    )

    assert spec.enable_task_loop is True
    assert spec.progressive_tool is not None
    assert spec.progressive_tool.enabled is False


def test_code_spec_materializes_through_registered_providers(tmp_path):
    adapter = _FakeCodeAdapter()
    config_base = {
        "react": {
            "enable_task_loop": False,
            "max_iterations": 3,
            "subagents": {"code_agent": {"enabled": False}},
        },
        "modes": {"code": {"rails": ["SkillUseRail"], "tools": []}},
    }
    sysop_card = SysOperationCard(
        id="code-spec-materialize",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None),
    )
    Runner.resource_mgr.remove_sys_operation(sysop_card.id)
    result = Runner.resource_mgr.add_sys_operation(sysop_card)
    assert result.is_ok()
    sysop = Runner.resource_mgr.get_sys_operation(sysop_card.id)

    try:
        spec, context = convert_code_config_to_deep_agent_spec(
            adapter=adapter,
            config_base=config_base,
            react_config=config_base["react"],
            model=_model(),
            card=AgentCard(id="code-spec-runtime", name="code-spec-runtime"),
            system_prompt="code prompt",
            workspace_root=str(tmp_path),
            project_dir=str(tmp_path),
            sys_operation=sysop,
            sys_operation_card=sysop_card,
            language="en",
            context_engine_config=None,
            kv_cache_affinity_config=None,
            enable_read_image_multimodal=False,
            completion_timeout=45.0,
        )

        # The converted spec/context is a snapshot. Later config mutations are
        # picked up only by the next conversion (the hot-reload path).
        config_base["modes"]["code"]["rails"] = ["WorktreeRail"]
        config_base["modes"]["code"]["tools"] = ["unexpected"]
        config_base["react"]["subagents"] = {"code_agent": {"enabled": True}}

        agent = spec.build(context)

        assert adapter.calls == ["rails", "subagents"]
        assert adapter.rail_names == ["SkillUseRail"]
        assert adapter.subagent_config == {"code_agent": {"enabled": False}}
        assert context.artifacts.rails == []
        assert context.artifacts.tools == []
        assert context.artifacts.subagents == []
        assert agent.deep_config.card.id == "code-spec-runtime"
        assert agent.deep_config.workspace.root_path == str(tmp_path)
        assert agent.deep_config.completion_timeout == 45.0
    finally:
        Runner.resource_mgr.remove_sys_operation(sysop_card.id)


def test_code_spec_preserves_configured_tool_owner(monkeypatch, tmp_path):
    adapter = _FakeCodeAdapter()
    tool = SimpleNamespace(
        card=ToolCard(
            id="owned_tool",
            name="owned_tool",
            description="test owned tool",
            stateless=False,
        )
    )
    adapter._get_tool_build_func = lambda _name, _agent_id: tool
    registered: list[tuple[object, str]] = []

    def register_owned_tool(instance, owner_id):
        instance.card.id = f"{instance.card.name}_{owner_id}"
        registered.append((instance, owner_id))

    monkeypatch.setattr(code_spec_module, "register_tool", register_owned_tool)
    config_base = {
        "react": {"enable_task_loop": False},
        "modes": {"code": {"rails": [], "tools": ["owned_tool"]}},
    }
    sysop_card = SysOperationCard(
        id="code-spec-tool-owner",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None),
    )
    Runner.resource_mgr.remove_sys_operation(sysop_card.id)
    result = Runner.resource_mgr.add_sys_operation(sysop_card)
    assert result.is_ok()

    try:
        spec, context = convert_code_config_to_deep_agent_spec(
            adapter=adapter,
            config_base=config_base,
            react_config=config_base["react"],
            model=_model(),
            card=AgentCard(id="code-spec-owner", name="code-spec-owner"),
            system_prompt="code prompt",
            workspace_root=str(tmp_path),
            project_dir=str(tmp_path),
            sys_operation=Runner.resource_mgr.get_sys_operation(sysop_card.id),
            sys_operation_card=sysop_card,
            language="en",
            context_engine_config=None,
            kv_cache_affinity_config=None,
            enable_read_image_multimodal=False,
            completion_timeout=None,
        )

        agent = spec.build(context)

        assert registered == [(tool, "code-spec-test-agent")]
        assert context.artifacts.tools == [tool]
        assert agent.ability_manager.get("owned_tool").id == (
            "owned_tool_code-spec-test-agent"
        )
    finally:
        Runner.resource_mgr.remove_sys_operation(sysop_card.id)


@pytest.mark.asyncio
async def test_code_adapter_builds_caller_supplied_spec_directly(
    monkeypatch,
    tmp_path,
):
    config_base = {
        "react": {"agent_name": "config-agent"},
        "modes": {"code": {"rails": [], "tools": []}},
        "symphony": {"enabled": True},
    }
    custom_model = _model()
    custom_spec = DeepAgentSpec(
        model=ModelSpec(
            model_client_config=custom_model.model_client_config,
            model_request_config=custom_model.model_config,
        ),
        card=AgentCard(id="caller-spec", name="caller-spec-agent"),
        system_prompt="caller supplied prompt",
        workspace=WorkspaceSpec(root_path=str(tmp_path / "spec-workspace")),
        max_iterations=9,
    )
    owner_ids: list[str] = []

    async def ensure_initialized() -> None:
        return None

    captured: dict = {}

    def build(spec, context):
        captured["spec"] = spec
        captured["context"] = context
        return SimpleNamespace(
            ensure_initialized=ensure_initialized,
            _registered_rails=[],
            deep_config=SimpleNamespace(
                model=spec.model.build(),
                tool_owner_id=None,
                tools=[],
            ),
            ability_manager=SimpleNamespace(set_owner_id=owner_ids.append),
            configured_rails=lambda: [],
        )

    adapter = interface_code.JiuwenSwarmCodeAdapter()
    create_model = MagicMock(side_effect=AssertionError("config model must not build"))

    def create_sys_operation():
        adapter._sys_operation_card = SysOperationCard(
            id="caller-spec-sysop",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(shell_allowlist=None),
        )
        return SimpleNamespace(id="caller-spec-sysop")

    monkeypatch.setattr(interface_code, "get_config", lambda: config_base)
    monkeypatch.setattr(
        interface_code,
        "get_agent_workspace_dir",
        lambda: tmp_path / "agent-workspace",
    )
    monkeypatch.setattr(DeepAgentSpec, "build", build)
    monkeypatch.setattr(adapter, "set_checkpoint", AsyncMock())
    monkeypatch.setattr(adapter, "_skip_own_instance_build", lambda: False)
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", MagicMock())
    monkeypatch.setattr(adapter, "_create_model", create_model)
    monkeypatch.setattr(adapter, "_create_sys_operation", create_sys_operation)
    monkeypatch.setattr(adapter, "_seed_runtime_cwd", MagicMock())
    monkeypatch.setattr(adapter, "_ensure_cron_tools_registered", MagicMock())
    sync_symphony = MagicMock()
    monkeypatch.setattr(
        adapter,
        "_sync_symphony_tools_for_runtime",
        sync_symphony,
    )
    monkeypatch.setattr(adapter, "_register_mcp_servers_from_config", AsyncMock())
    monkeypatch.setattr(adapter, "_load_active_packages", AsyncMock())
    monkeypatch.setattr(adapter, "load_user_rails", AsyncMock())
    convert_config = MagicMock()
    monkeypatch.setattr(
        interface_code.code_agent_spec,
        "convert_code_config_to_deep_agent_spec",
        convert_config,
    )

    await adapter.create_instance(
        {"channel_id": "web", "project_dir": str(tmp_path / "project")},
        spec=custom_spec,
    )

    effective_spec = captured["spec"]
    context = captured["context"]
    assert effective_spec is adapter._code_agent_spec
    assert effective_spec is not custom_spec
    assert effective_spec.system_prompt == "caller supplied prompt"
    assert effective_spec.max_iterations == 9
    assert effective_spec.sys_operation.id == "caller-spec-sysop"
    assert isinstance(context, code_spec_module.CodeBuildContext)
    assert context.adapter is adapter
    assert context.project_dir == str(tmp_path / "project")
    assert adapter._session_instance_spec is not custom_spec
    assert adapter._session_instance_spec.model_dump() == custom_spec.model_dump()
    assert adapter._custom_code_spec_active is True
    assert adapter._agent_name == "caller-spec-agent"
    assert owner_ids == [adapter._tool_owner_id()]
    assert adapter._default_model_name == "test-model"
    create_model.assert_not_called()
    convert_config.assert_not_called()
    sync_symphony.assert_not_called()


def test_custom_code_build_context_is_cloned_and_rebound(tmp_path):
    root_adapter = interface_code.JiuwenSwarmCodeAdapter()
    child_adapter = interface_code.JiuwenSwarmCodeAdapter()
    child_adapter.mark_as_session_scoped("session-1")
    child_adapter._project_dir = str(tmp_path / "runtime-project")
    child_adapter._channel_id = "web"
    supplied = code_spec_module.CodeBuildContext(
        adapter=root_adapter,
        config_base={"custom": True},
        react_config={"max_iterations": 5},
        tool_owner_id="caller-owner",
        session_id="caller-session",
        channel_id="tui",
        project_dir=str(tmp_path / "caller-project"),
    )
    supplied.artifacts.tools.append(object())

    prepared = child_adapter._prepare_custom_code_build_context(
        supplied,
        {"react": {}},
        {},
    )

    assert prepared is not supplied
    assert prepared.adapter is child_adapter
    assert prepared.tool_owner_id == child_adapter._tool_owner_id()
    assert prepared.session_id == "session-1"
    assert prepared.channel_id == "web"
    assert prepared.project_dir == str(tmp_path / "caller-project")
    assert prepared.config_base == {"custom": True}
    assert prepared.artifacts.tools == []
    assert supplied.adapter is root_adapter
    assert supplied.artifacts.tools != []


def test_custom_spec_is_propagated_to_deferred_and_session_builds():
    adapter = interface_code.JiuwenSwarmCodeAdapter()
    custom_spec = DeepAgentSpec(system_prompt="custom")
    custom_context = BuildContext(extras={"caller": "test"})
    adapter._session_instance_spec = custom_spec
    adapter._session_instance_build_context = custom_context

    assert adapter._session_instance_extra_create_kwargs() == {
        "spec": custom_spec,
        "build_context": custom_context,
    }


@pytest.mark.asyncio
async def test_code_adapter_rejects_build_context_without_custom_spec():
    adapter = interface_code.JiuwenSwarmCodeAdapter()

    with pytest.raises(ValueError, match="requires a custom spec"):
        await adapter.create_instance(build_context=BuildContext())
