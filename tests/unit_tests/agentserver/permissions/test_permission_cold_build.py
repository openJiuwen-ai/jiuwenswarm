"""Cold Smart construction keeps real SDK registration and owner cleanup."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard
from openjiuwen.harness import DeepAgent

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import compose_host_effective_permissions
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


class _OwnedProbe(Tool):
    def __init__(self, name):
        super().__init__(ToolCard(id=name, name=name, description="Cold-build owned probe"))

    async def invoke(self, inputs, **kwargs):
        return inputs

    async def stream(self, inputs, **kwargs):
        yield inputs


class _ColdBuild:
    def __init__(self, adapter, root):
        self.adapter, self.root = adapter, root
        self.revision = 1
        self.raw = {
            "react": {"workspace_dir": str(root), "enable_task_loop": False},
            "models": {"default": {"model_client_config": {"model_name": "cold-build-model"}}},
            "permissions": {
                "enabled": True, "mode": "auto", "defaults": {"*": "ask"},
                "tools": {"bash": "ask"}, "file_guard": {"enabled": False},
                "auto": {"reviewer_timeout_ms": 7890},
            },
        }
        self.user = {"deny_tools": ["user_denied"]}
        self.session = {"allow_tools": ["session_allowed"]}
        self.model = Model(
            model_client_config=ModelClientConfig(
                # Test-only reserved endpoint; this fixture never calls the model.
                client_provider="OpenAI", api_key="test-only", api_base="https://model.invalid/v1",
            ),
            model_config=ModelRequestConfig(model_name="cold-build-model"),
        )
        self.sysop = SysOperation(SysOperationCard(
            id=f"cold-sysop-{root.name}", work_config=LocalWorkConfig(sandbox_root=[str(root)]),
        ))
        self.tool = _OwnedProbe(f"cold_probe_{root.name}")
        self.shared_tool = _OwnedProbe(f"shared_probe_{root.name}")
        self.shared_tool.card.stateless = True
        self.instances = []
        self.helper_calls = []
        self.events = []

    def capture(self):
        global_layer = deepcopy(self.raw["permissions"])
        effective = compose_host_effective_permissions(
            global_permissions=global_layer, user_permissions=self.user, session_permissions=self.session,
        )
        return f"cold-{self.revision}", global_layer, effective

    def change(self):
        self.revision += 1
        self.user = {"deny_tools": [f"latest_{self.revision}"]}

    async def create(self):
        await self.adapter.create_instance(
            {"channel_id": "web", "project_dir": str(self.root)}, mode="agent",
        )

    def callbacks(self, instance):
        if instance.react_agent is None:
            return []
        event = instance.react_agent.agent_callback_manager._get_agent_event(AgentCallbackEvent.BEFORE_TOOL_CALL)
        return Runner.callback_framework.list_callbacks(event)


@pytest.fixture
async def cold(tmp_path, monkeypatch):
    adapter = interface_deep.JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped(f"cold-{tmp_path.name}")
    h = _ColdBuild(adapter, tmp_path)
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(h.raw))
    monkeypatch.setattr(interface_deep, "load_dotenv_runtime", Mock())
    monkeypatch.setattr(interface_deep, "get_runtime_state_path", lambda _sid=None: tmp_path / "runtime.yaml")
    monkeypatch.setattr(adapter, "_capture_permission_version", h.capture)
    monkeypatch.setattr(adapter, "_refresh_multimodal_configs", Mock())
    monkeypatch.setattr(adapter, "_resolve_enable_read_image_multimodal", lambda _config: False)
    monkeypatch.setattr(adapter, "_resolve_enable_subagent_runtime", lambda _config: False)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(adapter, "_resolve_prompt_language", lambda: "en")
    monkeypatch.setattr(adapter, "_skill_retrieval_tools_enabled_for_runtime", lambda _config: False)
    monkeypatch.setattr(adapter, "_create_sys_operation", lambda: h.sysop)
    monkeypatch.setattr(adapter, "_build_subagents_with_general_purpose", lambda **_kwargs: [])
    monkeypatch.setattr(adapter, "_ensure_cron_tools_registered", Mock())
    monkeypatch.setattr(adapter, "_sync_a2x_runtime_state", Mock())
    for name in ("set_checkpoint", "_try_init_a2x_client", "_load_active_packages", "load_user_rails"):
        monkeypatch.setattr(adapter, name, AsyncMock())

    # Keep the actual rail assembly owner and every permission/stream/ask builder.
    for name in (
        "runtime_prompt", "response_prompt", "multimodal_image", "task_planning", "security",
        "model_anomaly_detection", "heartbeat", "circuit_breaker", "avatar", "memory_forbidden",
        "subagent", "eternal_conversation", "filesystem", "skill", "skill_retrieval_prompt",
        "symphony_orchestration", "work_agent_mode", "work_plan_approval",
    ):
        monkeypatch.setattr(adapter, f"_build_{name}_rail", lambda **_kwargs: None)
    monkeypatch.setattr(interface_deep, "_build_context_processor_rail", lambda **_kwargs: None)

    def create_model(_config):
        adapter._model = h.model
        return h.model

    async def tool_cards(owner):
        assert adapter._instance is None
        adapter._register_agent_owned_tool(h.tool, owner)
        adapter._register_agent_owned_tool(h.shared_tool, owner)
        assert Runner.resource_mgr.get_tool(h.tool.card.id) is h.tool
        h.events.append("tool_registered_before_instance")
        return [h.tool.card, h.shared_tool.card]

    monkeypatch.setattr(adapter, "_create_model", create_model)
    monkeypatch.setattr(adapter, "_get_tool_cards", tool_cards)
    original_factory = interface_deep.create_deep_agent
    original_builder = interface_deep.build_permission_rail
    original_configure = DeepAgent.configure
    original_ensure = DeepAgent.ensure_initialized

    def factory(*args, **kwargs):
        instance = original_factory(*args, **kwargs)
        h.instances.append(instance)
        return instance

    def builder(*args, **kwargs):
        h.helper_calls.append(dict(kwargs))
        return original_builder(*args, **kwargs)

    def configure(instance, *args, **kwargs):
        h.events.append("configure")
        return original_configure(instance, *args, **kwargs)

    async def ensure(instance, *args, **kwargs):
        h.events.append("ensure")
        return await original_ensure(instance, *args, **kwargs)

    monkeypatch.setattr(interface_deep, "create_deep_agent", factory)
    monkeypatch.setattr(interface_deep, "build_permission_rail", builder)
    monkeypatch.setattr(DeepAgent, "configure", configure)
    monkeypatch.setattr(DeepAgent, "ensure_initialized", ensure)
    try:
        yield h
    finally:
        for instance in h.instances:
            for rail in list(instance.configured_rails()):
                try:
                    await instance.unregister_rail(rail)
                except BaseException:
                    pass
            await instance._agent_callback_manager.clear()
            if instance.react_agent is not None:
                await instance.react_agent.agent_callback_manager.clear()
            instance.ability_manager.teardown_tools()
        # A failed assertion must not leave the deliberately registered probe behind.
        if Runner.resource_mgr.get_tool(h.tool.card.id) is not None:
            Runner.resource_mgr.remove_tool(h.tool.card.id)
        if Runner.resource_mgr.get_tool(h.shared_tool.card.id) is not None:
            Runner.resource_mgr.remove_tool(h.shared_tool.card.id)


@pytest.mark.asyncio
async def test_cold_create_uses_one_snapshot_and_consistent_model_session_sysop(cold, monkeypatch):
    h = cold

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("cold installed policy reloaded an overlay")

    monkeypatch.setattr(permissions_layers, "load_user_permissions", unexpected_read)
    monkeypatch.setattr(permissions_layers, "load_session_permissions", unexpected_read)
    await h.create()
    adapter, instance = h.adapter, h.adapter._instance
    assert isinstance(instance, DeepAgent) and instance in h.instances
    assert h.events.index("tool_registered_before_instance") < h.events.index("configure")
    assert "ensure" in h.events
    assert instance.deep_config.model is h.model and adapter._model is h.model
    assert instance.deep_config.sys_operation is h.sysop
    assert isinstance(adapter._permission_rail, AutoPermissionInterruptRail)
    assert adapter._permission_rail.sys_operation is h.sysop
    assert adapter._permission_rail.workspace_root == h.root
    assert isinstance(adapter._stream_event_rail, JiuSwarmStreamEventRail)
    assert isinstance(adapter._ask_user_rail, StructuredAskUserRail)
    assert adapter._ask_user_rail._strict_continuation_contract is True
    assert adapter._stream_event_rail._root_permission_queue is adapter._root_permission_queue
    for attr in ("_permission_rail", "_stream_event_rail", "_ask_user_rail", "_root_context_rail",
                 "_root_permission_queue_rail", "_root_permission_completion_rail"):
        assert instance.is_registered_rail(getattr(adapter, attr))
    call = h.helper_calls[0]
    assert call["llm"] is h.model and call["sys_operation"] is h.sysop
    assert call["model_name"] == h.model.model_config.model_name
    assert call["session_id"] == adapter._parent_session_id
    assert h.tool.card.id.endswith(adapter._tool_owner_id())
    assert adapter._root_context_rail._command_sys_operation is h.sysop
    assert call["installed_permissions"] == h.capture()[2]
    assert adapter._permission_state.permission_epoch == h.capture()[0]
    assert len(h.callbacks(instance)) >= 2
    assert Runner.resource_mgr.get_tool(h.tool.card.id) is h.tool
    reviewer = adapter._permission_rail.auto_reviewer.client._model
    assert reviewer is not h.model and reviewer.model_config == h.model.model_config
    installed = adapter._permission_rail.base_rail._host.get_permissions_snapshot()
    h.change()
    assert adapter._permission_rail.base_rail._host.get_permissions_snapshot() == installed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,sub_mode", [("unknown", None), ("agent", "unknown")])
async def test_unknown_mode_builds_without_smart_activation(cold, mode, sub_mode):
    h = cold
    await h.adapter.create_instance(
        {"channel_id": "web", "project_dir": str(h.root)}, mode=mode, sub_mode=sub_mode,
    )
    assert isinstance(h.adapter._instance, DeepAgent)
    assert not h.adapter._enable_auto_permission
    assert not h.adapter._instance.find_rails_by_type(AutoPermissionInterruptRail)
    assert h.adapter._root_permission_queue_rail is None
    assert h.adapter._permission_state.permission_epoch is None


@pytest.mark.asyncio
async def test_cold_registration_policy_change_installs_latest_version(cold, monkeypatch):
    h = cold
    original = DeepAgent._register_rail_selective
    changed = False

    async def register(instance, rail):
        nonlocal changed
        await original(instance, rail)
        if isinstance(rail, AutoPermissionInterruptRail) and not changed:
            changed = True
            h.change()

    monkeypatch.setattr(DeepAgent, "_register_rail_selective", register)
    await h.create()
    assert changed
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]
    assert f"latest_{h.revision}" in h.adapter._permission_rail.permission_config["deny_tools"]
    assert len(h.helper_calls) == 2
    assert h.adapter._instance.find_rails_by_type(AutoPermissionInterruptRail) == [h.adapter._permission_rail]
    assert h.callbacks(h.adapter._instance)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["tool_discovery", "permission_prepare", "ensure"])
async def test_cold_failure_removes_owned_tool_and_real_callbacks(cold, monkeypatch, failure):
    h = cold
    if failure == "tool_discovery":
        discover = h.adapter._get_tool_cards

        async def failed_discovery(owner):
            await discover(owner)
            raise RuntimeError("discovery failure")

        monkeypatch.setattr(h.adapter, "_get_tool_cards", failed_discovery)
    elif failure == "permission_prepare":
        monkeypatch.setattr(interface_deep, "build_permission_rail", Mock(side_effect=RuntimeError("prepare failure")))
    else:
        original = DeepAgent.ensure_initialized

        async def fail_ensure(instance, *args, **kwargs):
            await original(instance, *args, **kwargs)
            raise RuntimeError("ensure failure")

        monkeypatch.setattr(DeepAgent, "ensure_initialized", fail_ensure)
    with pytest.raises(RuntimeError, match="failure"):
        await h.create()
    assert h.events[0] == "tool_registered_before_instance"
    assert h.adapter._permission_state.permission_isolated
    assert h.adapter._permission_state.permission_cleanup_complete
    assert Runner.resource_mgr.get_tool(h.tool.card.id) is None
    assert Runner.resource_mgr.get_tool(h.shared_tool.card.id) is h.shared_tool
    for instance in h.instances:
        assert instance.configured_rails() == []
        assert h.callbacks(instance) == []
    if failure != "ensure":
        assert h.adapter._instance is None and not h.instances
