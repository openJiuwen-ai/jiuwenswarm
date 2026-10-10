"""Real SDK coverage for root coordination without session Smart ownership."""

# Imported pytest fixtures intentionally share test parameter names.
# ruff: noqa: F811

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, Model
from openjiuwen.harness import DeepAgent

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_context_rail import RootContextRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionCompletionRail,
    RootPermissionQueueRail,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.permissions.test_permission_admission_edges import built_edges  # noqa: F401


SMART_ROOT_TYPES = (
    AutoPermissionInterruptRail, RootContextRail, RootPermissionQueueRail,
    RootPermissionCompletionRail,
)


async def _root(factory, monkeypatch, *, mode="auto", built=True):
    h = await factory("root")
    h.adapter._is_session_scoped_adapter = False
    h.adapter._parent_session_id = None
    h.raw["permissions"]["mode"] = mode

    def no_session_snapshot():
        raise AssertionError("a non-session root must not capture a Smart epoch")

    monkeypatch.setattr(h.adapter, "_capture_permission_version", no_session_snapshot)
    await h.create()
    assert h.adapter._instance is None
    if built:
        await h.adapter.ensure_instance()
    return h


def _assert_root_graph(h):
    adapter, instance = h.adapter, h.adapter._instance
    assert not adapter._is_session_scoped_adapter
    assert not adapter._enable_auto_permission and adapter._permission_state.permission_epoch is None
    assert not adapter._permission_state.permission_isolated
    if instance is not None:
        assert isinstance(instance, DeepAgent)
        assert not any(isinstance(rail, SMART_ROOT_TYPES) for rail in instance.configured_rails())
        assert instance.is_registered_rail(adapter._permission_rail)
        assert instance.is_registered_rail(adapter._stream_event_rail)
        assert instance.is_registered_rail(adapter._ask_user_rail)
        assert not adapter._ask_user_rail._strict_continuation_contract
        assert h.callbacks(instance)


def _graph(h):
    instance = h.adapter._instance
    return (tuple(instance.configured_rails()), tuple(h.callbacks(instance))) if instance else ((), ())


async def _child(factory, parent, monkeypatch, *, name="existing", mode="manual"):
    h = await factory(name)
    h.raw["permissions"]["mode"] = mode
    await h.create()
    sid = h.adapter._parent_session_id
    parent.adapter._session_adapters[sid] = h.adapter
    parent.adapter._session_adapter_versions[sid] = 0
    # All owners see one global config, while capture remains session-specific.
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(parent.raw))
    return h


def _desired(root, children, mode):
    root.raw["permissions"]["mode"] = mode
    root.raw["permissions"]["tools"]["latest_policy_probe"] = "deny"
    for child in children:
        child.raw["permissions"] = deepcopy(root.raw["permissions"])
        child.revision += 1
    return deepcopy(root.raw)


def _request(child):
    return AgentRequest(
        request_id="root-owner-input", session_id=child.adapter._parent_session_id,
        channel_id="web", req_method=ReqMethod.CHAT_SEND,
        params={"query": "Continue", "mode": "agent", "project_dir": str(child.root)},
    )


async def _invoke_public_child(child, monkeypatch):
    calls = []

    async def model_invoke(_model, *args, **kwargs):
        calls.append((args, kwargs))
        return AssistantMessage(content="root owner test complete")

    async def terminal(_request, inputs):
        return await child.adapter._instance.invoke(inputs)

    monkeypatch.setattr(Model, "invoke", model_invoke)
    monkeypatch.setattr(child.adapter, "_process_message_impl", terminal)
    result = await child.adapter.process_message_impl(_request(child), {"query": "Continue"})
    assert "root owner test complete" in str(result) and len(calls) == 1


def _full_reload_providers(h, monkeypatch):
    """Keep actual root rail assembly/configure, only isolate optional services."""
    adapter = h.adapter

    async def snapshot(config, _env):
        adapter._config_base_cache = deepcopy(config)
        adapter._config_cache = deepcopy(config.get("react", {}))
        return config

    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", snapshot)
    monkeypatch.setattr(adapter, "_build_skill_rail", lambda *_args, **_kwargs: None)
    for name in (
        "_cancel_skill_retrieval_build_after_reload", "_try_init_a2x_client",
        "_sync_skill_retrieval_prompt_rail_for_runtime", "load_user_rails",
        "_sync_mcp_servers_for_runtime", "_load_active_packages", "_handle_memory_rail_by_config",
    ):
        monkeypatch.setattr(adapter, name, AsyncMock())
    for name in (
        "_sync_a2x_runtime_state", "_sync_multimodal_tools_for_runtime",
        "_sync_paid_search_tool_for_runtime", "_sync_symphony_tools_for_runtime",
        "_sync_skill_retrieval_tools_for_runtime", "_sync_active_evolution_review_agent_after_reload",
    ):
        monkeypatch.setattr(adapter, name, Mock())


@pytest.mark.asyncio
async def test_smart_config_cold_root_ensure_preserves_ordinary_sdk_owner(built_edges, monkeypatch):
    root = await _root(built_edges, monkeypatch)
    _assert_root_graph(root)
    assert root.adapter._instance in root.instances
    assert "configure" in root.events and "ensure" in root.events
    assert root.adapter._config_base_cache["permissions"]["mode"] == "auto"
    assert root.helper_calls and all(not call["enable_auto_permission"] for call in root.helper_calls)
    assert all(call.get("installed_permissions") is None for call in root.helper_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("built", [False, True])
@pytest.mark.parametrize("transition", ["enter", "exit"])
async def test_root_permission_notification_preserves_graph_and_publishes_child_desired(
    built_edges, monkeypatch, built, transition,
):
    old_mode, new_mode = ("manual", "auto") if transition == "enter" else ("auto", "manual")
    root = await _root(built_edges, monkeypatch, mode=old_mode, built=built)
    child = await _child(built_edges, root, monkeypatch, mode=old_mode)
    old_root_graph, old_child_graph = _graph(root), _graph(child)
    old_epoch = child.adapter._permission_state.permission_epoch
    desired = _desired(root, [child], new_mode)
    sid = child.adapter._parent_session_id
    child.adapter._mark_session_active(sid)
    try:
        await root.adapter.reload_agent_config(desired, reload_scopes={"permissions"})
        assert _graph(root) == old_root_graph and _graph(child) == old_child_graph
        assert child.adapter._permission_state.permission_epoch == old_epoch
        assert root.adapter._config_base_cache["permissions"] == desired["permissions"]
        assert root.adapter._pending_session_reload_config_base == desired
        assert root.adapter._session_adapter_versions[sid] < root.adapter._session_adapter_config_version
        # Internal/approval lookups cannot install the newly published D.
        assert await root.adapter._get_or_create_session_adapter(sid) is child.adapter
        assert _graph(child) == old_child_graph and child.adapter._permission_state.permission_epoch == old_epoch
    finally:
        child.adapter._unmark_session_active(sid)
    admitted = await root.adapter._get_session_adapter_for_request(_request(child), reserve_activity=False)
    assert admitted is child.adapter
    assert child.adapter._enable_auto_permission is (new_mode == "auto")
    assert child.adapter._permission_state.permission_epoch == (child.capture()[0] if new_mode == "auto" else None)
    assert root.adapter._session_adapter_versions[sid] == root.adapter._session_adapter_config_version
    assert _graph(root) == old_root_graph
    _assert_root_graph(root)
    await _invoke_public_child(child, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("built", [False, True])
async def test_future_child_cold_build_uses_latest_root_pending_desired(built_edges, monkeypatch, built):
    root = await _root(built_edges, monkeypatch, mode="manual", built=built)
    first = _desired(root, [], "auto")
    await root.adapter.reload_agent_config(first, reload_scopes={"permissions"})
    root.raw["permissions"]["tools"]["latest_policy_probe"] = "allow"
    latest = deepcopy(root.raw)
    await root.adapter.reload_agent_config(latest, reload_scopes={"permissions"})
    assert root.adapter._session_adapters == {}
    assert root.adapter._pending_session_reload_config_base == latest
    future = await built_edges("future")
    future.raw["permissions"] = deepcopy(latest["permissions"])
    future.revision = 3
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(latest))
    monkeypatch.setattr(root.adapter, "_new_session_scoped_adapter", lambda _sid: future.adapter)
    sid = future.adapter._parent_session_id
    try:
        child = await root.adapter._get_or_create_session_adapter(sid, host_external_input=True)
        assert child is future.adapter and isinstance(child._instance, DeepAgent)
        assert child._instance in future.instances
        assert child._enable_auto_permission and child._permission_state.permission_epoch == future.capture()[0]
        assert child._instance.is_registered_rail(child._permission_rail)
        assert child._config_base_cache["permissions"]["tools"]["latest_policy_probe"] == "allow"
        installed = [call for call in future.helper_calls if call.get("session_id") == sid]
        assert installed[-1]["installed_permissions"] == future.capture()[2]
        # The real permission builder normalizes unconditional allow to ask in
        # Smart mode; desired propagation must be checked before that projection.
        assert child._permission_rail.permission_config["tools"]["latest_policy_probe"] == "ask"
        assert root.adapter._session_adapter_versions[sid] == root.adapter._session_adapter_config_version
        _assert_root_graph(root)
        # Use the same project authority that the parent supplied during build.
        future.root = child._permission_workspace_root
        await _invoke_public_child(future, monkeypatch)
    finally:
        await future.adapter.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("built", [False, True])
async def test_root_exit_without_cached_children_does_not_leave_future_smart_desired(
    built_edges, monkeypatch, built,
):
    root = await _root(built_edges, monkeypatch, built=built)
    await root.adapter.reload_agent_config(deepcopy(root.raw), reload_scopes={"permissions"})
    _full_reload_providers(root, monkeypatch)
    desired = _desired(root, [], "manual")
    await root.adapter.reload_agent_config(desired, reload_scopes={"permissions"})
    # With no installed Smart child E and a manual candidate, approved root
    # coordination defers to develop's ordinary reload, not a root Smart epoch.
    if built:
        await root.adapter._instance.ensure_initialized()
    _assert_root_graph(root)
    assert root.adapter._session_adapters == {}
    assert root.adapter._pending_session_reload_config_base == desired
    future = await built_edges("future-manual")
    future.raw["permissions"] = deepcopy(desired["permissions"])
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(desired))
    monkeypatch.setattr(root.adapter, "_new_session_scoped_adapter", lambda _sid: future.adapter)
    _full_reload_providers(future, monkeypatch)
    sid = future.adapter._parent_session_id
    try:
        child = await root.adapter._get_or_create_session_adapter(sid, host_external_input=True)
        await child._instance.ensure_initialized()
        assert child is future.adapter and isinstance(child._instance, DeepAgent)
        assert not child._enable_auto_permission and child._permission_state.permission_epoch is None
        assert not any(isinstance(rail, SMART_ROOT_TYPES) for rail in child._instance.configured_rails())
        assert child._instance.is_registered_rail(child._permission_rail)
        assert child._config_base_cache["permissions"] == desired["permissions"]
        assert root.adapter._session_adapter_versions[sid] == root.adapter._session_adapter_config_version
        future.root = child._permission_workspace_root
        await _invoke_public_child(future, monkeypatch)
        _assert_root_graph(root)
    finally:
        await future.adapter.cleanup()


@pytest.mark.asyncio
async def test_full_root_reload_keeps_real_manual_graph_and_fans_out_desired(built_edges, monkeypatch):
    root = await _root(built_edges, monkeypatch, mode="manual")
    child = await _child(built_edges, root, monkeypatch, mode="manual")
    _full_reload_providers(root, monkeypatch)
    desired = _desired(root, [child], "auto")
    desired["react"]["agent_name"] = "root-after-full-reload"
    root.raw = deepcopy(desired)
    instance = root.adapter._instance
    original_configure = instance.configure
    configured = []

    def configure(config):
        configured.append(config)
        return original_configure(config)

    monkeypatch.setattr(instance, "configure", configure)
    await root.adapter.reload_agent_config(desired, reload_scopes={"models", "permissions"})
    # develop configures lazily; initialization exercises actual SDK retirement
    # and registration instead of inferring membership from prepared lists.
    await instance.ensure_initialized()
    assert configured and root.adapter._instance is instance
    assert root.adapter._agent_name == "root-after-full-reload"
    _assert_root_graph(root)
    assert root.adapter._pending_session_reload_config_base == desired
    assert not child.adapter._enable_auto_permission
    sid = child.adapter._parent_session_id
    assert root.adapter._session_adapter_versions[sid] < root.adapter._session_adapter_config_version
