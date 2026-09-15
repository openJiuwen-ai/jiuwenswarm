"""Permission publication through public Host entry points and the real SDK graph."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.harness import DeepAgent, DeepAgentConfig
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import PermissionInterruptRequest
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import compose_host_effective_permissions
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


class _Lifecycle:
    def __init__(self, adapter, instance):
        self.adapter, self.instance = adapter, instance
        self.revision = 0
        self.desired = {}
        self.observed = []

    def change(self, mode="auto"):
        self.revision += 1
        self.desired = {"permissions": {
            "enabled": True, "mode": mode, "defaults": {"*": "ask"},
            "tools": {"bash": "ask", f"revision_{self.revision}": "deny"},
            "file_guard": {"enabled": False},
        }}
        return self.desired

    def capture(self):
        global_layer = deepcopy(self.desired["permissions"])
        effective = compose_host_effective_permissions(
            global_permissions=global_layer, user_permissions={}, session_permissions={},
        )
        return f"epoch-{self.revision}", global_layer, effective

    def permissions(self):
        return self.instance.find_rails_by_type((PermissionInterruptRail, AutoPermissionInterruptRail))

    def callbacks(self):
        event = self.instance.react_agent.agent_callback_manager._get_agent_event(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
        )
        return Runner.callback_framework.list_callbacks(event)

    async def reload(self):
        await self.adapter.reload_agent_config(self.desired, reload_scopes={"permissions"})

    async def request(self, *, stream=False, answer=False, request_id="new"):
        request = AgentRequest(
            request_id=request_id, session_id="lifecycle", channel_id="web",
            req_method=ReqMethod.CHAT_SEND, params={"mode": "agent", "query": "inspect"},
        )
        inputs = {"query": InteractiveInput("answer") if answer else "inspect"}
        if stream:
            return [item async for item in self.adapter.process_message_stream_impl(request, inputs)]
        return await self.adapter.process_message_impl(request, inputs)


@pytest.fixture
async def lifecycle(tmp_path, monkeypatch):
    adapter = interface_deep.JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("lifecycle")
    adapter._session_instance_mode = "agent"
    adapter._session_instance_sub_mode = ""
    adapter._workspace_dir = tmp_path
    adapter._permission_workspace_root = tmp_path
    adapter._sys_operation = object()
    adapter._model = object()
    agent = DeepAgent(AgentCard(name=f"permission-lifecycle-{tmp_path.name}"))
    h = _Lifecycle(adapter, agent)
    h.change("manual")
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(h.desired))
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: {})
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _: {})
    monkeypatch.setattr(adapter, "_capture_permission_version", h.capture)
    adapter._config_base_cache = deepcopy(h.desired)
    adapter._permission_rail = build_permission_rail(h.desired, session_id="lifecycle")
    adapter._stream_event_rail = JiuSwarmStreamEventRail()
    adapter._ask_user_rail = StructuredAskUserRail()
    agent.configure(DeepAgentConfig(
        rails=[adapter._permission_rail, adapter._stream_event_rail, adapter._ask_user_rail],
        auto_create_workspace=False,
    ))
    await agent.ensure_initialized()
    adapter._instance = agent

    async def terminal(*_args):
        observation = (adapter._permission_state.permission_epoch, h.permissions(), h.callbacks())
        h.observed.append(observation)
        assert len(observation[1]) == 1
        assert observation[2], "registered rails must also have SDK callbacks"
        return observation

    async def terminal_stream(*args):
        yield await terminal(*args)

    monkeypatch.setattr(adapter, "_process_message_impl", AsyncMock(side_effect=terminal))
    monkeypatch.setattr(adapter, "_process_message_stream_impl", terminal_stream)
    try:
        yield h
    finally:
        # Cleanup uses real SDK unregister/manager operations, including after failures.
        for rail in list(agent.configured_rails()):
            try:
                await agent.unregister_rail(rail)
            except BaseException:
                pass
        await agent._agent_callback_manager.clear()
        if agent.react_agent is not None:
            await agent.react_agent.agent_callback_manager.clear()
        agent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_manual_smart_manual_replaces_complete_registered_group(lifecycle, monkeypatch):
    h = lifecycle
    adapter = h.adapter
    old = adapter._permission_rail
    model, sysop = adapter._model, adapter._sys_operation
    monkeypatch.setattr(adapter, "_create_model", Mock(side_effect=AssertionError("model rebuilt")))
    monkeypatch.setattr(adapter, "_sync_mcp_servers_for_runtime", AsyncMock(side_effect=AssertionError("MCP rebuilt")))
    initial_callbacks = len(h.callbacks())
    h.change()
    await h.reload()
    assert not h.instance.is_registered_rail(old)
    assert h.permissions() == [adapter._permission_rail]
    assert isinstance(adapter._permission_rail, AutoPermissionInterruptRail)
    assert adapter._stream_event_rail._root_permission_queue is adapter._root_permission_queue
    assert adapter._ask_user_rail._strict_continuation_contract is True
    assert len(h.callbacks()) > initial_callbacks
    assert adapter._permission_state.permission_epoch == h.capture()[0]
    installed = adapter._permission_rail
    h.change()
    await h.reload()
    assert adapter._permission_rail is not installed
    assert not h.instance.is_registered_rail(installed)
    assert adapter._model is model and adapter._sys_operation is sysop
    h.change("manual")
    await h.reload()
    assert type(adapter._permission_rail) is PermissionInterruptRail
    assert adapter._permission_state.permission_epoch is None and not adapter._enable_auto_permission
    assert adapter._stream_event_rail._root_permission_queue is None
    assert adapter._ask_user_rail._strict_continuation_contract is False
    assert len(h.callbacks()) == initial_callbacks
    for attr in ("_root_context_rail", "_root_permission_queue_rail", "_root_permission_completion_rail"):
        assert getattr(adapter, attr) is None
    await h.request()


async def test_pending_manual_owner_does_not_allocate_smart_binding(lifecycle, monkeypatch):
    h = lifecycle
    resolver = Mock(side_effect=AssertionError("must not prepare Smart paths yet"))
    monkeypatch.setattr(interface_deep, "bind_session_runtime_workspace", resolver)
    # Remaining in manual must preserve the original admission behavior.
    await h.request()
    assert not resolver.called
    assert h.adapter._permission_state.permission_epoch is None
    h.change("auto")
    monkeypatch.setattr(h.adapter, "_permission_work_pending", lambda _sid: True)
    with pytest.raises(RuntimeError, match="permission_session_busy"):
        await h.request()
    assert not resolver.called
    assert not h.adapter._enable_auto_permission
    assert h.adapter._permission_runtime_paths is None


async def test_snapshot_is_read_only_and_requires_prepared_smart_binding(lifecycle, monkeypatch):
    h = lifecycle
    h.change("auto")
    monkeypatch.setattr(permissions_layers, "capture_permission_layers", lambda _sid: (
        deepcopy(h.desired["permissions"]), {}, {}, {},
    ))
    binder = Mock(side_effect=AssertionError("snapshot must not allocate"))
    monkeypatch.setattr(interface_deep, "bind_session_runtime_workspace", binder)
    capture = interface_deep.JiuWenSwarmDeepAdapter._capture_permission_version
    with pytest.raises(RuntimeError, match="permission_workspace_binding_unprepared"):
        capture(h.adapter)
    binder.assert_not_called()
    assert h.adapter._permission_runtime_paths is None
    assert not h.adapter._enable_auto_permission


async def test_installed_reload_requires_binding_without_reallocation(lifecycle, monkeypatch):
    h = lifecycle
    h.change("auto")
    await h.reload()
    binding = h.adapter._permission_runtime_paths
    old_epoch, old_graph = h.adapter._permission_state.permission_epoch, h.permissions()
    binder = Mock(side_effect=AssertionError("reload must not repair missing state"))
    monkeypatch.setattr(interface_deep, "bind_session_runtime_workspace", binder)
    h.adapter._permission_runtime_paths = None
    h.change("auto")
    try:
        with pytest.raises(RuntimeError, match="permission_workspace_binding_unprepared"):
            await h.reload()
        binder.assert_not_called()
        assert h.adapter._permission_state.permission_epoch == old_epoch
        assert h.permissions() == old_graph
    finally:
        h.adapter._permission_runtime_paths = binding


async def test_failed_smart_preparation_retries_with_same_binding(lifecycle, monkeypatch):
    h = lifecycle
    original_builder = interface_deep.build_permission_rail
    binder = Mock(wraps=interface_deep.bind_session_runtime_workspace)
    monkeypatch.setattr(interface_deep, "bind_session_runtime_workspace", binder)
    monkeypatch.setattr(interface_deep, "build_permission_rail", Mock(side_effect=ValueError("prepare failed")))
    old_epoch, old_graph = h.adapter._permission_state.permission_epoch, h.permissions()
    h.change("auto")
    with pytest.raises(ValueError, match="prepare failed"):
        await h.reload()
    binding = h.adapter._permission_runtime_paths
    assert binding is not None and binder.call_count == 1
    assert h.adapter._permission_state.permission_epoch == old_epoch
    assert h.permissions() == old_graph
    assert not h.adapter._enable_auto_permission
    h.change("auto")
    monkeypatch.setattr(interface_deep, "build_permission_rail", original_builder)
    await h.reload()
    assert h.adapter._permission_runtime_paths is binding
    assert h.adapter._permission_workspace_root == binding.runtime_workspace_root
    assert binder.call_count == 1
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["unregister_rail", "register_rail", "ensure_initialized"])
@pytest.mark.parametrize("stream", [False, True])
async def test_public_request_waits_at_real_sdk_mutation_boundary(lifecycle, monkeypatch, boundary, stream):
    h = lifecycle
    original = getattr(h.instance, boundary)
    reached, release = asyncio.Event(), asyncio.Event()
    blocked = False

    async def pause(*args, **kwargs):
        nonlocal blocked
        result = await original(*args, **kwargs)
        if not blocked:
            blocked = True
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(h.instance, boundary, pause)
    h.change()
    reloading = asyncio.create_task(h.reload())
    await asyncio.wait_for(reached.wait(), 5)
    requesting = asyncio.create_task(h.request(stream=stream))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not requesting.done()
        assert h.observed == []
        release.set()
        await asyncio.wait_for(reloading, 5)
        await asyncio.wait_for(requesting, 5)
        assert len(h.observed) == 1
        epoch, rails, callbacks = h.observed[0]
        assert epoch == h.capture()[0]
        assert rails == h.permissions() and callbacks == h.callbacks()
    finally:
        release.set()
        await asyncio.gather(reloading, requesting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["unregister_rail", "register_rail", "ensure_initialized"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_sdk_mutation_isolates_borrowed_adapter_and_clears_callbacks(
    lifecycle, monkeypatch, boundary, cancelled,
):
    h = lifecycle
    borrowed = h.adapter
    original = getattr(h.instance, boundary)
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        result = await original(*args, **kwargs)
        if not failed:
            failed = True
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("injected SDK mutation failure")
        return result

    monkeypatch.setattr(h.instance, boundary, fail_once)
    h.change()
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await h.reload()
    assert borrowed._permission_state.permission_isolated
    assert borrowed._permission_state.permission_cleanup_complete
    assert h.instance.configured_rails() == []
    assert h.callbacks() == []
    with pytest.raises(RuntimeError, match="permission_session_isolated"):
        await h.request()
    with pytest.raises(RuntimeError, match="permission_session_isolated"):
        await h.request(stream=True, answer=True)
    assert h.observed == []


@pytest.mark.asyncio
async def test_task_cancel_after_real_registration_cleans_partial_group(lifecycle, monkeypatch):
    h = lifecycle
    original = h.instance.register_rail
    registered = asyncio.Event()

    async def pause(rail):
        await original(rail)
        registered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(h.instance, "register_rail", pause)
    h.change()
    task = asyncio.create_task(h.reload())
    try:
        await asyncio.wait_for(registered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert h.adapter._permission_state.permission_isolated and h.adapter._permission_state.permission_cleanup_complete
        assert h.instance.configured_rails() == [] and h.callbacks() == []
        with pytest.raises(RuntimeError, match="permission_session_isolated"):
            await h.request()
        assert h.observed == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_prepare_failure_preserves_old_graph_but_blocks_new_work(lifecycle, monkeypatch):
    h = lifecycle
    h.change()
    await h.reload()
    old_epoch, old_graph, old_callbacks = h.adapter._permission_state.permission_epoch, h.permissions(), h.callbacks()
    h.change()
    monkeypatch.setattr(interface_deep, "build_permission_rail", Mock(side_effect=ValueError("prepare failed")))
    with pytest.raises(ValueError, match="prepare failed"):
        await h.reload()
    assert not h.adapter._permission_state.permission_isolated
    assert h.permissions() == old_graph and h.callbacks() == old_callbacks
    with pytest.raises(ValueError, match="prepare failed"):
        await h.request()
    assert h.observed == []
    await h.request(answer=True)
    assert h.observed[0][0] == old_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_busy_or_pending_owner_keeps_epoch_and_accepts_old_answer(lifecycle, monkeypatch, pending):
    h = lifecycle
    h.change()
    await h.reload()
    old_epoch, old_graph = h.adapter._permission_state.permission_epoch, h.permissions()
    release, entered = asyncio.Event(), asyncio.Event()
    active = None
    card = None
    if pending:
        card = h.adapter._root_permission_queue.begin(
            root_session_id="lifecycle", request_id="pending", execution_session_id="lifecycle",
            tool_call_id="call", tool_name="bash",
        )
        h.adapter._root_permission_queue.mark_pending(
            card.key, request=PermissionInterruptRequest(message="Approve", payload_schema={},
                metadata={}, tool_name="bash", tool_call_id="call"),
            auto_manual=False, root_context=None,
        )
    else:
        terminal = h.adapter._process_message_impl

        async def active_terminal(request, inputs):
            if request.request_id == "active":
                entered.set()
                await release.wait()
            return await terminal(request, inputs)

        monkeypatch.setattr(h.adapter, "_process_message_impl", active_terminal)
        active = asyncio.create_task(h.request(request_id="active"))
        await asyncio.wait_for(entered.wait(), 5)
    try:
        h.change()
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.reload()
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.request()
        await asyncio.wait_for(h.request(answer=True), 5)
        assert h.observed[0][0] == old_epoch and h.permissions() == old_graph
    finally:
        if card is not None:
            h.adapter._root_permission_queue.finish(card.key)
        release.set()
        if active is not None:
            await active
    await h.reload()
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("continuous", [False, True])
async def test_policy_change_during_registration_retries_or_isolates(lifecycle, monkeypatch, continuous):
    h = lifecycle
    original = h.instance.register_rail
    registrations = 0

    async def changing_policy(rail):
        nonlocal registrations
        await original(rail)
        if isinstance(rail, AutoPermissionInterruptRail):
            registrations += 1
            if continuous or registrations == 1:
                h.change()

    monkeypatch.setattr(h.instance, "register_rail", changing_policy)
    h.change()
    if continuous:
        with pytest.raises(RuntimeError, match="permission_policy_not_stable"):
            await h.reload()
        assert registrations == 3
        assert h.adapter._permission_state.permission_isolated and h.adapter._permission_state.permission_cleanup_complete
        assert h.callbacks() == []
        assert h.instance.configured_rails() == []
    else:
        await h.reload()
        assert registrations == 2
        assert h.adapter._permission_state.permission_epoch == h.capture()[0]
        assert f"revision_{h.revision}" in h.adapter._permission_rail.permission_config["tools"]
        await h.request()


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["enter_with_stale_config", "exit"])
async def test_transition_waits_and_does_not_reinstall_after_lock(lifecycle, monkeypatch, transition):
    h = lifecycle
    old_manual = deepcopy(h.desired)
    if transition == "exit":
        h.change()
        await h.reload()
        h.change("manual")
    else:
        h.change()
        monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(old_manual))
    replacement = AsyncMock(wraps=h.adapter._replace_permission_group)
    monkeypatch.setattr(h.adapter, "_replace_permission_group", replacement)
    original = h.instance.register_rail
    reached, release = asyncio.Event(), asyncio.Event()
    paused = False

    async def pause(rail):
        nonlocal paused
        await original(rail)
        if not paused:
            paused = True
            reached.set()
            await release.wait()

    monkeypatch.setattr(h.instance, "register_rail", pause)
    reloading = asyncio.create_task(h.reload())
    requesting = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        requesting = asyncio.create_task(h.request())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not requesting.done() and h.observed == []
        release.set()
        await asyncio.wait_for(reloading, 5)
        await asyncio.wait_for(requesting, 5)
        replacement.assert_awaited_once()
        assert len(h.observed) == 1
        assert isinstance(h.permissions()[0], AutoPermissionInterruptRail) == (transition != "exit")
    finally:
        release.set()
        await asyncio.gather(reloading, *([requesting] if requesting else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["session", "workspace"])
async def test_invalid_owner_is_rejected_before_installation(lifecycle, monkeypatch, invalid):
    h = lifecycle
    old_graph, old_callbacks = h.permissions(), h.callbacks()
    old_epoch, old_enabled = h.adapter._permission_state.permission_epoch, h.adapter._enable_auto_permission
    h.change()
    builder = Mock(wraps=interface_deep.build_permission_rail)
    monkeypatch.setattr(interface_deep, "build_permission_rail", builder)
    params = {"mode": "agent", "query": "inspect"}
    if invalid == "workspace":
        params["workspace_dir"] = str(h.adapter._permission_workspace_root / "different")
    request = AgentRequest(
        request_id="invalid", session_id="foreign" if invalid == "session" else "lifecycle",
        channel_id="web", req_method=ReqMethod.CHAT_SEND, params=params,
    )
    expected = "session_owner_mismatch" if invalid == "session" else "workspace_changed"
    with pytest.raises(interface_deep.RootPermissionQueueError, match=expected):
        await h.adapter.process_message_impl(request, {"query": "inspect"})
    builder.assert_not_called()
    assert h.permissions() == old_graph and h.callbacks() == old_callbacks
    assert h.adapter._permission_state.permission_epoch == old_epoch
    assert h.adapter._enable_auto_permission == old_enabled
    assert h.observed == []


@pytest.mark.asyncio
async def test_real_capture_fingerprint_includes_user_and_session(lifecycle, monkeypatch):
    h = lifecycle
    user, session = {}, {}
    monkeypatch.setattr(permissions_layers, "capture_permission_layers", lambda _sid: (
        deepcopy(h.desired["permissions"]), deepcopy(user), deepcopy(session), {},
    ))
    capture = interface_deep.JiuWenSwarmDeepAdapter._capture_permission_version
    initial = capture(h.adapter)[0]
    user["deny_tools"] = ["user_tool"]
    user_epoch = capture(h.adapter)[0]
    session["allow_tools"] = ["session_tool"]
    session_epoch = capture(h.adapter)[0]
    assert len({initial, user_epoch, session_epoch}) == 3
