"""GP cutover preserves old children and publishes real SDK child definitions."""

# Imported pytest fixtures intentionally share test parameter names.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import DeepAgent
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401


def _gp(instance):
    return next(spec for spec in instance.deep_config.subagents if spec.agent_card.name == "general-purpose")


def _one(instance, rail_type):
    matches = instance.find_rails_by_type(rail_type)
    assert len(matches) == 1
    assert instance.is_registered_rail(matches[0])
    return matches[0]


def _neutral(instance):
    stream = _one(instance, JiuSwarmStreamEventRail)
    ask = _one(instance, StructuredAskUserRail)
    assert stream._root_permission_queue is None
    assert ask._strict_continuation_contract is False
    return stream, ask


@pytest.fixture
async def gp(cold, monkeypatch):
    h = cold
    adapter = h.adapter
    original = interface_deep.JiuWenSwarmDeepAdapter._build_subagents_with_general_purpose
    monkeypatch.setattr(adapter, "_build_subagents_with_general_purpose", original.__get__(adapter))
    h.other_spec = SubAgentConfig(agent_card=AgentCard(name="unchanged-specialist"), system_prompt="")
    monkeypatch.setattr(adapter, "_build_configured_subagents", lambda *_args: ([h.other_spec], True))
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: h.user)
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: h.session)
    h.children = []

    def child(name):
        # No child factory/configure/registered-list mock: exercise the pinned SDK.
        instance = adapter._instance.create_subagent("general-purpose", name)
        h.children.append(instance)
        return instance

    h.child = child
    try:
        yield h
    finally:
        for instance in reversed(h.children):
            for rail in list(instance.configured_rails()):
                try:
                    await instance.unregister_rail(rail)
                except BaseException:
                    pass
            await instance._agent_callback_manager.clear()
            if instance.react_agent is not None:
                await instance.react_agent.agent_callback_manager.clear()
            instance.ability_manager.teardown_tools()


async def _reload(h, mode="auto"):
    h.raw["permissions"]["mode"] = mode
    h.change()
    await h.adapter.reload_agent_config(h.raw, reload_scopes={"permissions"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_cold_gp_has_correct_permission_and_actual_parent_workspace(gp, mode):
    h = gp
    h.raw["permissions"]["mode"] = mode
    await h.create()
    parent = h.adapter._instance
    spec = _gp(parent)
    assert spec.workspace is parent.deep_config.workspace
    assert spec.sys_operation is parent.deep_config.sys_operation is h.sysop
    assert spec.model is h.model
    assert any(item is h.other_spec for item in parent.deep_config.subagents)
    child = h.child(f"cold-{mode}")
    await child.ensure_initialized()
    assert child.deep_config.workspace is parent.deep_config.workspace
    assert child.deep_config.sys_operation is h.sysop
    assert child.deep_config.model is h.model
    _neutral(child)
    assert not child.find_rails_by_type((
        AutoPermissionInterruptRail, interface_deep.RootPermissionQueueRail,
        interface_deep.RootContextRail, interface_deep.RootPermissionCompletionRail,
    ))
    if mode == "manual":
        assert _one(child, PermissionInterruptRail) is h.adapter._permission_rail
    else:
        assert not child.find_rails_by_type(PermissionInterruptRail)
    assert h.callbacks(child), "SDK callbacks must exist, not just configured rail objects"


@pytest.mark.asyncio
async def test_manual_smart_manual_publishes_new_spec_without_mutating_existing_child(gp, monkeypatch):
    h = gp
    h.raw["permissions"]["mode"] = "manual"
    await h.create()
    adapter, parent = h.adapter, h.adapter._instance
    old_list, old_spec = parent.deep_config.subagents, _gp(parent)
    old_items = tuple(old_list)
    old_rails = tuple(old_spec.rails)
    old_child = h.child("manual-existing")
    await old_child.ensure_initialized()
    old_stream, old_ask = _neutral(old_child)
    config, model, sysop, workspace = parent.deep_config, adapter._model, adapter._sys_operation, parent.deep_config.workspace
    tools = tuple(adapter._tool_cards)
    configure = Mock(wraps=parent.configure)
    monkeypatch.setattr(parent, "configure", configure)

    await _reload(h)
    smart_list, smart_spec = parent.deep_config.subagents, _gp(parent)
    assert smart_list is not old_list and smart_spec is not old_spec
    assert tuple(old_spec.rails) == old_rails
    assert tuple(old_list) == old_items
    assert any(spec is h.other_spec for spec in smart_list)
    assert adapter._stream_event_rail is not old_stream
    assert adapter._ask_user_rail is not old_ask
    assert _neutral(old_child) == (old_stream, old_ask)
    for rail in (adapter._stream_event_rail, adapter._ask_user_rail):
        assert parent.is_registered_rail(rail)
    smart_child = h.child("smart-created")
    await smart_child.ensure_initialized()
    smart_stream, smart_ask = _neutral(smart_child)
    assert smart_stream is not adapter._stream_event_rail
    assert smart_ask is not adapter._ask_user_rail
    assert not smart_child.find_rails_by_type((PermissionInterruptRail, AutoPermissionInterruptRail))
    smart_rails = tuple(smart_spec.rails)

    await _reload(h, "manual")
    manual_spec = _gp(parent)
    assert manual_spec is not smart_spec and manual_spec is not old_spec
    assert tuple(smart_spec.rails) == smart_rails
    assert tuple(old_spec.rails) == old_rails
    assert _neutral(old_child) == (old_stream, old_ask)
    assert _neutral(smart_child) == (smart_stream, smart_ask)
    new_child = h.child("manual-after-exit")
    await new_child.ensure_initialized()
    _one(new_child, PermissionInterruptRail)
    _neutral(new_child)
    assert parent.deep_config is config
    assert parent.deep_config.model is model is adapter._model
    assert parent.deep_config.workspace is workspace
    assert adapter._sys_operation is sysop is parent.deep_config.sys_operation
    assert tuple(adapter._tool_cards) == tools
    assert any(spec is h.other_spec for spec in parent.deep_config.subagents)
    assert manual_spec.workspace is workspace and manual_spec.sys_operation is sysop
    configure.assert_not_called()
    assert h.callbacks(parent) and h.callbacks(new_child)


@pytest.mark.asyncio
async def test_two_smart_children_ensure_concurrently_with_independent_neutral_rails(gp, monkeypatch):
    h = gp
    await h.create()
    parent = h.adapter._instance
    left, right = h.child("concurrent-left"), h.child("concurrent-right")
    entered, release = asyncio.Event(), asyncio.Event()
    original = DeepAgent._register_rail_selective
    seen = set()

    async def register(instance, rail):
        await original(instance, rail)
        if instance in (left, right) and isinstance(rail, StructuredAskUserRail):
            seen.add(id(instance))
            if len(seen) == 2:
                entered.set()
            await release.wait()

    monkeypatch.setattr(DeepAgent, "_register_rail_selective", register)
    tasks = [asyncio.create_task(child.ensure_initialized()) for child in (left, right)]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert all(not task.done() for task in tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    streams, asks = zip(*[_neutral(child) for child in (left, right)])
    assert streams[0] is not streams[1] and asks[0] is not asks[1]
    assert streams[0]._deep_agent is left and streams[1]._deep_agent is right
    templates = _gp(parent).rails
    assert all(all(rail is not template for template in templates) for rail in (*streams, *asks))
    registered_tools = [ask.get_structured_tools()[0] for ask in asks]
    assert registered_tools[0] is not registered_tools[1]
    assert all(h.callbacks(child) for child in (left, right))
    # SDK GP card/tool-owner reuse is a separate baseline diagnostic, not a new
    # tool-ownership contract introduced by the neutral rail fork.
    await left.unregister_rail(asks[0])
    assert asks[0].get_structured_tools() == []
    assert asks[1].get_structured_tools() == [registered_tools[1]]
    assert right.is_registered_rail(asks[1]) and h.callbacks(right)


@pytest.mark.asyncio
async def test_registration_retry_publishes_gp_only_after_latest_graph_is_ready(gp, monkeypatch):
    h = gp
    await h.create()
    parent = h.adapter._instance
    old_list, old_spec = parent.deep_config.subagents, _gp(parent)
    old_snapshot, old_epoch = h.adapter._general_purpose_rail_snapshot, h.adapter._permission_state.permission_epoch
    entered, release = asyncio.Event(), asyncio.Event()
    original = parent.register_rail
    blocked = False

    async def register(rail):
        nonlocal blocked
        await original(rail)
        if isinstance(rail, AutoPermissionInterruptRail) and not blocked:
            blocked = True
            entered.set()
            await release.wait()

    monkeypatch.setattr(parent, "register_rail", register)
    task = asyncio.create_task(_reload(h))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert parent.deep_config.subagents is old_list
        assert _gp(parent) is old_spec
        assert h.adapter._general_purpose_rail_snapshot is old_snapshot
        assert h.adapter._permission_state.permission_epoch == old_epoch
        h.change()
    finally:
        release.set()
        await task
    assert _gp(parent) is not old_spec
    assert h.adapter._permission_state.permission_epoch == h.capture()[0]
    assert len(h.helper_calls) == 3  # Cold, first candidate, latest candidate.
    assert any(spec is h.other_spec for spec in parent.deep_config.subagents)
    assert tuple(old_spec.rails) == old_snapshot
    child = h.child("after-retry")
    await child.ensure_initialized()
    _neutral(child)
    assert not child.find_rails_by_type((PermissionInterruptRail, AutoPermissionInterruptRail))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["prepare", "register", "cancel"])
async def test_failed_cutover_does_not_publish_or_mutate_old_gp(gp, monkeypatch, failure):
    h = gp
    await h.create()
    parent = h.adapter._instance
    old_list, old_spec = parent.deep_config.subagents, _gp(parent)
    old_snapshot = h.adapter._general_purpose_rail_snapshot
    entered = asyncio.Event()
    if failure == "prepare":
        monkeypatch.setattr(interface_deep, "build_permission_rail", Mock(side_effect=RuntimeError("prepare failed")))
    else:
        original = parent.register_rail

        async def register(rail):
            await original(rail)
            if isinstance(rail, StructuredAskUserRail):
                if failure == "register":
                    raise RuntimeError("register failed")
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(parent, "register_rail", register)
    task = asyncio.create_task(_reload(h))
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await task
    assert parent.deep_config.subagents is old_list
    assert _gp(parent) is old_spec
    assert tuple(old_spec.rails) == old_snapshot
    assert h.adapter._general_purpose_rail_snapshot is old_snapshot
    if failure == "prepare":
        assert not h.adapter._permission_state.permission_isolated
        assert h.callbacks(parent)
    else:
        assert h.adapter._permission_state.permission_isolated and h.adapter._permission_state.permission_cleanup_complete
        assert parent.configured_rails() == []
        assert h.callbacks(parent) == []
