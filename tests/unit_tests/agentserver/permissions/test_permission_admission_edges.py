"""Owner and settlement edges exercised with real SDK graphs and execution."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, Model
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.tools.subagent._control_registry import get_subagent_control

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold


@pytest.fixture
async def built_edges(tmp_path, monkeypatch):
    """Reuse cold construction providers, including real configure and cleanup.

    Multiple independent generations let parent reconstruction run the actual
    create_instance implementation instead of returning a prebuilt fake child.
    """
    generations = []

    async def create(name):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        generator = cold.__wrapped__(root, monkeypatch)
        h = await anext(generator)
        generations.append(generator)
        return h

    try:
        yield create
    finally:
        for generator in reversed(generations):
            await generator.aclose()


class _PauseBeforeInvoke(AgentRail):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def before_invoke(self, ctx):
        self.entered.set()
        await self.release.wait()


def _script_model(monkeypatch):
    calls = []

    async def invoke(self, *args, **kwargs):
        calls.append((args, kwargs))
        return AssistantMessage(content="edge completed")

    monkeypatch.setattr(Model, "invoke", invoke)
    return calls


def _parent(h):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    sid = h.adapter._parent_session_id
    parent._session_adapters[sid] = h.adapter
    parent._session_adapter_versions[sid] = 0
    return parent


def _request(h):
    return AgentRequest(
        request_id="edge-request", channel_id="web", session_id=h.adapter._parent_session_id,
        req_method=ReqMethod.CHAT_SEND, params={"query": "continue", "mode": "agent"},
    )


@pytest.mark.asyncio
async def test_built_root_does_not_acquire_session_smart_rails(built_edges):
    h = await built_edges("built-root")
    h.adapter._is_session_scoped_adapter = False
    h.adapter._parent_session_id = None
    h.raw["permissions"]["mode"] = "manual"
    await h.create()
    assert h.adapter._instance is None  # Normal root construction remains a router.
    instance = await h.adapter.ensure_instance()  # Real package/notify entry point.
    assert instance is not None and not h.adapter._enable_auto_permission
    old_graph = tuple(instance.configured_rails())
    old_callbacks = h.callbacks(instance)
    h.raw["permissions"]["mode"] = "auto"
    await h.adapter.reload_agent_config(deepcopy(h.raw), reload_scopes={"permissions"})
    assert not h.adapter._enable_auto_permission
    assert tuple(instance.configured_rails()) == old_graph
    assert h.callbacks(instance) == old_callbacks
    assert not h.adapter._enable_auto_permission
    assert not h.adapter._permission_state.permission_isolated


@pytest.mark.asyncio
async def test_sdk_callback_outliving_host_activity_blocks_reload(built_edges, monkeypatch):
    h = await built_edges("sdk-callback")
    calls = _script_model(monkeypatch)
    await h.create()
    instance = h.adapter._instance
    gate = _PauseBeforeInvoke()
    await instance.register_rail(gate)
    graph, callbacks = tuple(instance.configured_rails()), h.callbacks(instance)
    executing = asyncio.create_task(instance.invoke({"query": "continue"}))
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        assert instance.is_invoke_active
        assert not h.adapter._active_session_ids and not h.adapter._session_agent_tasks
        h.change()
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.adapter.reload_agent_config(deepcopy(h.raw), reload_scopes={"permissions"})
        assert tuple(instance.configured_rails()) == graph
        assert h.callbacks(instance) == callbacks and not h.adapter._permission_state.permission_isolated
        assert not calls
    finally:
        gate.release.set()
        result = await asyncio.wait_for(executing, 10)
    assert "edge completed" in str(result) and calls
    assert not instance.is_invoke_active
    await h.adapter.reload_agent_config(deepcopy(h.raw), reload_scopes={"permissions"})
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]


@pytest.mark.asyncio
async def test_live_sdk_background_subagent_blocks_reload_with_idle_host(built_edges, monkeypatch):
    h = await built_edges("sdk-background")
    _script_model(monkeypatch)
    await h.create()
    instance = h.adapter._instance
    gate = _PauseBeforeInvoke()
    instance.deep_config.subagents = [SubAgentConfig(
        agent_card=AgentCard(id="edge-worker", name="edge-worker", description="Execution probe"),
        system_prompt="Reply briefly.", model=h.model, rails=[gate],
    )]
    session = Session(session_id=h.adapter._parent_session_id, card=instance.card)
    control = get_subagent_control(instance, session)
    spawned = await control.spawn("edge-worker", "continue")
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        assert control.list_live()
        assert not control.get_status(spawned.subagent_id).is_final()
        assert not instance.is_invoke_active and instance.active_round is None
        assert not h.adapter._active_session_ids and not h.adapter._session_agent_tasks
        graph, callbacks = tuple(instance.configured_rails()), h.callbacks(instance)
        h.change()
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await h.adapter.reload_agent_config(deepcopy(h.raw), reload_scopes={"permissions"})
        assert tuple(instance.configured_rails()) == graph
        assert h.callbacks(instance) == callbacks and not h.adapter._permission_state.permission_isolated
    finally:
        gate.release.set()
        await control.cancel_all("edge_test_cleanup")
    assert control.get_status(spawned.subagent_id).is_final()
    await h.adapter.reload_agent_config(deepcopy(h.raw), reload_scopes={"permissions"})
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_owner", ["host_cleanup", "sdk_stop"])
async def test_persistent_cleanup_retains_parent_owner_until_real_rebuild(
    built_edges, monkeypatch, failure_owner,
):
    h = await built_edges("isolated-old")
    calls = _script_model(monkeypatch)
    await h.create()
    parent = _parent(h)
    sid = h.adapter._parent_session_id
    old_instance = h.adapter._instance
    register = old_instance.register_rail
    cleanup = h.adapter.cleanup
    stop = old_instance.stop
    blocked = True

    async def fail_after_mutation(rail):
        await register(rail)
        raise RuntimeError("touched registration failure")

    async def persistent_cleanup_failure():
        if blocked:
            raise RuntimeError("cleanup still unavailable")
        await cleanup()

    async def persistent_stop_failure(*args, **kwargs):
        if blocked:
            raise RuntimeError("SDK stop still unavailable")
        return await stop(*args, **kwargs)

    monkeypatch.setattr(old_instance, "register_rail", fail_after_mutation)
    if failure_owner == "host_cleanup":
        monkeypatch.setattr(h.adapter, "cleanup", persistent_cleanup_failure)
    else:
        monkeypatch.setattr(old_instance, "stop", persistent_stop_failure)
    h.change()
    with pytest.raises(RuntimeError, match="touched registration failure"):
        await h.adapter.reload_agent_config(deepcopy(h.raw), reload_scopes={"permissions"})
    assert h.adapter._permission_state.permission_isolated and not h.adapter._permission_state.permission_cleanup_complete
    assert not old_instance.configured_rails() and h.callbacks(old_instance) == []

    # Repeated parent lookups and explicit failed-owner eviction cannot bypass
    # incomplete cleanup. A borrowed child also remains unusable.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="permission_session_cleanup_pending"):
            await parent._get_or_create_session_adapter(sid)
        await parent._evict_failed_permission_session_adapter(sid, h.adapter)
        # TTL eviction must honor the same incomplete-cleanup boundary.
        await parent._evict_idle_session_adapters()
        assert parent._session_adapters[sid] is h.adapter
        with pytest.raises(RuntimeError, match="permission_session_isolated"):
            await h.adapter.process_message_impl(_request(h), {"query": "continue"})
    assert not calls

    replacement = await built_edges("isolated-old")
    assert replacement.adapter._parent_session_id == sid
    # Only inject optional providers via the cold fixture. Parent still invokes
    # the real cold create/SDK configure/register/initialize path on this child.
    monkeypatch.setattr(parent, "_new_session_scoped_adapter", lambda _sid: replacement.adapter)
    blocked = False
    fresh = await parent._get_or_create_session_adapter(sid)
    assert fresh is replacement.adapter and fresh is not h.adapter
    assert h.adapter._permission_state.permission_cleanup_complete
    assert fresh._instance is not None and fresh._instance is not old_instance
    assert fresh._instance in replacement.instances
    assert fresh._permission_state.permission_epoch == replacement.capture()[0]
    assert fresh._instance.is_registered_rail(fresh._permission_rail)
    assert replacement.callbacks(fresh._instance)

    # The public owner boundary is real; isolate unrelated chat orchestration,
    # then execute the actual freshly built SDK graph and scripted model.
    async def sdk_terminal(request, inputs):
        return await fresh._instance.invoke(inputs)

    monkeypatch.setattr(fresh, "_process_message_impl", sdk_terminal)
    result = await fresh.process_message_impl(_request(replacement), {"query": "continue"})
    assert "edge completed" in str(result) and calls
    assert parent._session_adapters[sid] is fresh and not fresh._permission_state.permission_isolated
    await fresh.cleanup()
