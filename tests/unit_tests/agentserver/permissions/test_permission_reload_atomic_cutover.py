"""Parent and manager reload contracts beyond direct-child SDK replacement."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime import agent_manager as manager_module
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.permissions.test_permission_lifecycle_integration import (
    lifecycle as lifecycle,
)


def _config(mode="auto", epoch="next"):
    return {"permissions": {"enabled": True, "mode": mode, "epoch": epoch}}


def _child():
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child.mark_as_session_scoped("session-a")
    return child


def _parent(child, session_id="session-a"):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    parent._session_adapters[session_id] = child
    parent._session_adapter_versions[session_id] = 0
    return parent


def _request():
    return AgentRequest(
        request_id="request-a", channel_id="web", session_id="session-a",
        req_method=ReqMethod.CHAT_SEND, params={"query": "continue", "mode": "agent"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.usefixtures("internal_auto_mode")
async def test_parent_lazy_permission_version_advances_only_on_external_input(external):
    child = _child()
    child.reload_agent_config = AsyncMock()
    parent = _parent(child)
    parent._mark_session_adapters_stale_for_reload(_config(), {})
    await parent._reload_session_adapter_if_stale(
        "session-a", child, host_external_input=external,
    )
    assert child.reload_agent_config.await_count == int(external)
    assert parent._session_adapter_versions["session-a"] == int(external)
    assert parent._pending_session_reload_config_base == _config()


@pytest.mark.asyncio
async def test_parent_busy_external_admission_retries_without_advancing_version(lifecycle):
    h = lifecycle
    h.change()
    await h.reload()
    parent = _parent(h.adapter, "lifecycle")
    old_epoch = h.adapter._permission_state.permission_epoch
    h.change()
    parent._mark_session_adapters_stale_for_reload(h.desired, {}, {"permissions"})
    h.adapter._mark_session_active("lifecycle")
    try:
        with pytest.raises(RuntimeError, match="permission_session_busy"):
            await parent._reload_session_adapter_if_stale(
                "lifecycle", h.adapter, host_external_input=True,
            )
        assert parent._session_adapter_versions["lifecycle"] == 0
        assert h.adapter._permission_state.permission_epoch == old_epoch
    finally:
        h.adapter._unmark_session_active("lifecycle")
    await parent._reload_session_adapter_if_stale(
        "lifecycle", h.adapter, host_external_input=True,
    )
    assert parent._session_adapter_versions["lifecycle"] == 1
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["lazy", "target"])
@pytest.mark.parametrize("failure", ["prepare", "touched", "cancelled"])
async def test_parent_reload_distinguishes_prepare_failure_from_isolated_child(
    lifecycle, monkeypatch, route, failure,
):
    h = lifecycle
    parent = _parent(h.adapter, "lifecycle")
    old_graph, old_callbacks = h.permissions(), h.callbacks()
    h.change()
    parent._mark_session_adapters_stale_for_reload(h.desired, {}, {"permissions"})
    if failure == "prepare":
        monkeypatch.setattr(interface_deep, "build_permission_rail",
                            Mock(side_effect=ValueError("prepare failed")))
    else:
        register = h.instance.register_rail
        failed = False

        async def mutate_then_fail(rail):
            nonlocal failed
            await register(rail)
            if not failed:
                failed = True
                if failure == "cancelled":
                    raise asyncio.CancelledError()
                raise RuntimeError("mutation failed")

        monkeypatch.setattr(h.instance, "register_rail", mutate_then_fail)
    error = asyncio.CancelledError if failure == "cancelled" else (
        ValueError if failure == "prepare" else RuntimeError
    )
    with pytest.raises(error):
        if route == "lazy":
            await parent._reload_session_adapter_if_stale(
                "lifecycle", h.adapter, host_external_input=True,
            )
        else:
            await parent._reload_target_session_adapter(
                h.desired, {}, target_session_id="lifecycle", reload_scopes={"permissions"},
            )
    assert parent._session_adapter_versions.get("lifecycle", 0) == 0
    if failure == "prepare":
        assert parent._session_adapters["lifecycle"] is h.adapter
        assert not h.adapter._permission_state.permission_isolated
        assert h.permissions() == old_graph and h.callbacks() == old_callbacks
    else:
        assert h.adapter._permission_state.permission_isolated and h.adapter._permission_state.permission_cleanup_complete
        assert h.callbacks() == [] and h.instance.configured_rails() == []
        with pytest.raises(RuntimeError, match="permission_session_isolated"):
            await h.request()
        # Prove stale cache lookup requests reconstruction, not full rebuilt execution.
        def request_rebuild(_session_id):
            raise RuntimeError("rebuild requested")

        monkeypatch.setattr(parent, "_new_session_scoped_adapter", request_rebuild)
        with pytest.raises(RuntimeError, match="rebuild requested"):
            await parent._get_or_create_session_adapter("lifecycle")
        assert "lifecycle" not in parent._session_adapters


@pytest.mark.asyncio
async def test_next_admission_installs_latest_root_pending_config():
    child, installed = _child(), []
    child._config_base_cache = _config("manual", "A")
    parent = _parent(child)
    parent._evict_idle_session_adapters = AsyncMock()

    async def reload(config, *_args, **_kwargs):
        child._config_base_cache = config
        installed.append(config["permissions"]["epoch"])

    async def terminal(_request, _inputs):
        return child._config_base_cache["permissions"]["epoch"]

    child.reload_agent_config, child.process_message_impl = reload, terminal
    parent._mark_session_adapters_stale_for_reload(_config("manual", "B"), {})
    parent._mark_session_adapters_stale_for_reload(_config("manual", "C"), {})
    assert await parent._process_message_impl(_request(), {}) == "C"
    assert installed == ["C"]
    assert parent._session_adapter_versions["session-a"] == 2


@pytest.mark.asyncio
async def test_targeted_transition_retries_after_old_owner_settles(lifecycle):
    h = lifecycle
    parent = _parent(h.adapter, "lifecycle")
    h.change()
    parent._mark_session_adapters_stale_for_reload(h.desired, {}, {"permissions"})
    h.adapter._mark_session_active("lifecycle")
    try:
        with pytest.raises(RuntimeError, match="permission_reload_deferred_manual_pending"):
            await parent._reload_target_session_adapter(
                h.desired, {}, target_session_id="lifecycle", reload_scopes={"permissions"},
            )
        assert not h.adapter._enable_auto_permission
        assert parent._session_adapter_versions["lifecycle"] == 0
    finally:
        h.adapter._unmark_session_active("lifecycle")
    await parent._reload_target_session_adapter(
        h.desired, {}, target_session_id="lifecycle", reload_scopes={"permissions"},
    )
    assert h.adapter._enable_auto_permission
    assert parent._session_adapter_versions["lifecycle"] == 1


@pytest.mark.asyncio
async def test_targeted_reload_completes_before_parent_lookup(lifecycle, monkeypatch):
    h = lifecycle
    parent = _parent(h.adapter, "lifecycle")
    h.change()
    register = h.instance.register_rail
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause(rail):
        await register(rail)
        reached.set()
        await release.wait()

    monkeypatch.setattr(h.instance, "register_rail", pause)
    reloading = asyncio.create_task(parent._reload_target_session_adapter(
        h.desired, {}, target_session_id="lifecycle", reload_scopes={"permissions"},
    ))
    lookup = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        lookup = asyncio.create_task(parent._get_or_create_session_adapter("lifecycle"))
        await asyncio.sleep(0)
        assert not lookup.done()
        release.set()
        await asyncio.wait_for(reloading, 5)
        assert await asyncio.wait_for(lookup, 5) is h.adapter
        assert h.adapter._permission_state.permission_epoch == h.capture()[0]
    finally:
        release.set()
        await asyncio.gather(reloading, *([lookup] if lookup else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_outer_reservation_balances_on_delegation_failure(failure):
    child, observed = _child(), []
    parent = _parent(child)

    async def terminal(_request, _inputs):
        raise failure

    async def evict():
        observed.append(child._active_session_ids.get("session-a", 0))

    child.process_message_impl = terminal
    parent._evict_idle_session_adapters = evict
    with pytest.raises(type(failure)):
        await parent._process_message_impl(_request(), {})
    assert observed == [0]
    assert not child.is_session_active("session-a")


@pytest.mark.asyncio
async def test_agent_manager_retries_identical_targeted_reload_after_deferral(monkeypatch):
    class DeferringAgent:
        pending, calls = True, 0

        async def reload_agent_config(self, **_kwargs):
            self.calls += 1
            if self.pending:
                raise RuntimeError("permission_reload_deferred_manual_pending")

    class TeamManager:
        async def update_evolution_config(self, _config):
            pass

    manager, agent = manager_module.AgentManager(), DeferringAgent()
    manager.agents = {"web": {"agent": agent}}
    monkeypatch.setattr(manager_module, "get_team_manager", lambda _: TeamManager())
    async def reload():
        await manager.reload_agents_config(
            _config(), {}, target_channel_id="web", target_session_id="session-a",
        )
    with pytest.raises(RuntimeError, match="permission_reload_deferred_manual_pending"):
        await reload()
    assert manager._last_reload_fingerprint is None
    agent.pending = False
    await reload()
    assert agent.calls == 2 and manager._last_reload_fingerprint is not None


def test_ordinary_manual_changes_do_not_acquire_smart_deferral():
    child = _child()
    child._config_base_cache = _config("manual", "old")
    child._is_session_live = lambda _: True
    assert not child._should_defer_permission_reload(
        _config("manual", "new"), session_id="session-a",
    )
