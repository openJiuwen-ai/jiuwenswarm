# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""User hooks follow delegated tools through the installed SDK lifecycle."""

import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig, SubAgentConfig
from openjiuwen.harness.subagents.explore_agent import build_explore_agent_config

from jiuwenswarm.common.hooks_config import HooksConfig, HookMatcher
from jiuwenswarm.server.hooks.user_hook_rail import UserHookRail


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_explore_child_bash_runs_through_hook(tmp_path, monkeypatch, blocked):
    log = tmp_path / "hooks.jsonl"
    command = f"cat >> {shlex.quote(str(log))}; " + ("echo denied >&2; exit 2" if blocked else "exit 0")
    config = HooksConfig(events={"PreToolUse": [
        HookMatcher(matcher="bash", hooks=[{"command": command, "timeout": 5}]),
    ]})
    rail = UserHookRail(config)
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(
        subagents=[build_explore_agent_config()], rails=[rail], workspace=str(tmp_path),
    ))
    # Exercise SDK child creation and callback dispatch without starting a model
    # or initializing unrelated filesystem/browser rails.
    rail.init(parent)
    child = parent.create_subagent("explore_agent", "child-session")
    inherited = [r for r in child.configured_rails() if isinstance(r, UserHookRail)]
    for child_rail in inherited:
        await child.register_rail(child_rail)
        child.remove_pending_rail(child_rail)
    execute = AsyncMock(return_value=("executed", None))
    manager = child._react_agent.ability_manager
    monkeypatch.setattr(manager, "_execute_single_tool_call", execute)
    session = SimpleNamespace(get_session_id=lambda: "child-session")
    ctx = AgentCallbackContext(agent=child._react_agent, session=session)
    await manager.execute(
        ctx=ctx,
        tool_call=ToolCall(id="bash-call", type="function", name="bash", arguments='{"command":"pwd"}'),
        session=session, parallel_tool_calls=False,
    )
    assert execute.await_count == (0 if blocked else 1)
    assert len(inherited) == 1
    payload = json.loads(log.read_text())
    assert payload["tool_name"] == "bash"
    assert payload["session_id"] == "child-session"
    assert payload["subagent_type"] == "explore_agent"


@pytest.mark.parametrize("name", [
    "explore_agent", "custom_agent",
])
def test_inherits_without_mutating_or_duplicating_specs(name):
    existing = object()
    spec = SubAgentConfig(agent_card=AgentCard(name=name), system_prompt="", rails=[existing])
    parent = SimpleNamespace(deep_config=SimpleNamespace(subagents=[spec]))
    rail = UserHookRail(HooksConfig())
    rail.init(parent)
    first = parent.deep_config.subagents
    rail.init(parent)
    assert parent.deep_config.subagents is first
    configured = parent.deep_config.subagents[0]
    assert spec.rails == [existing]
    assert configured.rails[0] is existing
    assert sum(isinstance(r, UserHookRail) for r in configured.rails) == 1
    assert configured.rails[-1] is not rail


@pytest.mark.asyncio
@pytest.mark.parametrize("event,method", [
    ("PreToolUse", "before_tool_call"), ("PostToolUse", "after_tool_call"),
    ("PostToolUseFailure", "on_tool_exception"), ("Stop", "after_invoke"),
])
async def test_child_identity_on_all_events(event, method):
    rail = UserHookRail(HooksConfig(events={event: [
        HookMatcher(matcher="*", hooks=[{"command": "true"}]),
    ]})).fork_for_agent()
    rail.init(SimpleNamespace(card=AgentCard(name="code_agent")))
    rail._executor.run_all = AsyncMock(return_value=[])
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="bash", tool_args={}, tool_result="ok", result="done"),
        session=SimpleNamespace(get_session_id=lambda: "child-2"), extra={},
    )
    await getattr(rail, method)(ctx)
    payload = rail._executor.run_all.await_args.kwargs["hook_input"]
    assert payload["session_id"] == "child-2"
    assert payload["subagent_type"] == "code_agent"


def test_preserves_custom_hooks_and_follows_parent_policy():
    """Simulate future policy replacement; the current host has no such caller."""
    custom = UserHookRail(HooksConfig())
    spec = SubAgentConfig(agent_card=AgentCard(name="code_agent"), system_prompt="", rails=[custom])
    parent = SimpleNamespace(deep_config=SimpleNamespace(subagents=[spec]))
    rail = UserHookRail(HooksConfig())
    rail.init(parent)
    configured = parent.deep_config.subagents[0]
    child = configured.rails[-1]
    executor = child._executor
    config = HooksConfig(events={"PreToolUse": [HookMatcher(matcher="bash", hooks=[{"command": "exit 2"}])]})
    rail._hooks_config = config
    rail.init(parent)
    assert child._config is config
    assert parent.deep_config.subagents[0] is configured
    assert child._executor is executor
    assert configured.rails[0] is custom
    assert spec.rails == [custom]
    config.events.clear()
    assert not child._config.events
    assert custom._config is not config


@pytest.mark.asyncio
async def test_reloaded_child_specs_inherit_without_reinitializing_rail():
    rail = UserHookRail(HooksConfig())
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(
        subagents=[build_explore_agent_config()], rails=[rail],
    ))
    rail.init(parent)
    # As in partial hot reload: keep the rail and replace the child registry.
    parent.configure(DeepAgentConfig(subagents=[build_explore_agent_config()], rails=[]))
    await rail.before_invoke(AgentCallbackContext(agent=parent))
    inherited = [r for r in parent.deep_config.subagents[0].rails if isinstance(r, UserHookRail)]
    assert len(inherited) == 1
    assert inherited[0]._config is rail._config


def test_forks_have_independent_blocking_state_and_executors():
    parent = UserHookRail(HooksConfig())
    first, second = parent.fork_for_agent(), parent.fork_for_agent()
    first._blocking_state.set({"reason": "blocked"})
    assert parent._blocking_state.get() is None
    assert second._blocking_state.get() is None
    assert len({id(r._executor) for r in (parent, first, second)}) == 3


def test_fork_exposes_read_only_parent_and_effective_policy():
    config = HooksConfig()
    parent = UserHookRail(config)
    child = parent.fork_for_agent()
    descendant = child.fork_for_agent()
    assert parent.parent_hook is None
    assert child.parent_hook is parent
    assert descendant.parent_hook is child
    assert descendant.hooks_config is config
    with pytest.raises(AttributeError):
        child.parent_hook = None
    with pytest.raises(AttributeError):
        child.hooks_config = HooksConfig()


def test_hooks_propagate_to_descendants():
    rail = UserHookRail(HooksConfig()).fork_for_agent()
    descendant = SubAgentConfig(agent_card=AgentCard(name="code_agent"), system_prompt="")
    child = SimpleNamespace(card=AgentCard(name="plan_agent"), deep_config=SimpleNamespace(subagents=[descendant]))
    rail.init(child)
    inherited = child.deep_config.subagents[0].rails[0]
    inherited.init(SimpleNamespace(card=AgentCard(name="code_agent")))
    assert inherited._subagent_type == "code_agent"
    assert rail._subagent_type == "plan_agent"


@pytest.mark.parametrize("mode", ["team", "team.plan.normal", "code.team", "team.plan.code"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_swarm_members_receive_user_hooks(mode, role):
    from jiuwenswarm.agents.swarm import SwarmBuildContext, registry
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
    from jiuwenswarm.agents.swarm.providers.code_rails import build_user_hooks

    config = {"hooks": {"PreToolUse": [{"matcher": "bash", "hooks": [{"type": "command", "command": "exit 2"}]}]}}
    rails, _ = build_member_capability_specs(config, mode=mode, role=role)
    specs = [spec for spec in rails if spec.type == registry.USER_HOOKS]
    assert len(specs) == 1
    rail = build_user_hooks(specs[0].params, SwarmBuildContext(member_name="researcher", mode=mode))
    assert isinstance(rail, UserHookRail)
    assert rail._subagent_type == "researcher"
    assert rail._config.events


@pytest.mark.parametrize("mode", ["team", "code.team"])
def test_swarm_empty_hooks_do_not_create_rail(mode):
    from jiuwenswarm.agents.swarm import SwarmBuildContext, registry
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
    from jiuwenswarm.agents.swarm.providers.code_rails import build_user_hooks

    rails, _ = build_member_capability_specs({}, mode=mode, role="teammate")
    spec = next(spec for spec in rails if spec.type == registry.USER_HOOKS)
    assert build_user_hooks(spec.params, SwarmBuildContext(mode=mode)) is None


async def _invoke_without_model(agent, monkeypatch):
    # Keep real SDK initialization and outer callback dispatch. Only replace
    # session persistence and model execution; no manually fabricated invoke ctx.
    monkeypatch.setattr(agent, "_prepare_single_round_session", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_run_single_round_invoke", AsyncMock(return_value={"output": "done"}))
    await agent.invoke({"query": "test"})


@pytest.mark.asyncio
@pytest.mark.parametrize("already_initialized", [False, True])
async def test_preconstructed_child_activation_and_policy_refresh(tmp_path, monkeypatch, already_initialized):
    """Exercise live attachment and simulate a future policy-reload integration."""
    child = DeepAgent(AgentCard(name="prebuilt")).configure(DeepAgentConfig())
    custom = UserHookRail(HooksConfig())
    child.add_rail(custom)
    if already_initialized:
        await _invoke_without_model(child, monkeypatch)
    config = HooksConfig()
    rail = UserHookRail(config)
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(subagents=[child], rails=[rail]))
    await _invoke_without_model(parent, monkeypatch)
    inherited = [r for r in child.configured_rails() if rail._owns(r)]
    assert len(inherited) == 1
    fork = inherited[0]
    assert child.is_registered_rail(fork)
    assert not child.is_pending_rail(fork)
    assert fork._subagent_type == "prebuilt"
    await _invoke_without_model(child, monkeypatch)
    await _invoke_without_model(parent, monkeypatch)
    assert [r for r in child.configured_rails() if rail._owns(r)] == [fork]
    assert custom in child.configured_rails()

    log = tmp_path / "prebuilt.jsonl"
    # An existing fork follows both replacement and in-place policy changes.
    rail._hooks_config = HooksConfig(events={"PreToolUse": [HookMatcher(
        matcher="bash", hooks=[{"command": f"cat >> {shlex.quote(str(log))}; exit 2"}],
    )]})
    await _invoke_without_model(parent, monkeypatch)
    execute = AsyncMock(return_value=("executed", None))
    manager = child._react_agent.ability_manager
    monkeypatch.setattr(manager, "_execute_single_tool_call", execute)
    session = SimpleNamespace(get_session_id=lambda: "prebuilt-session")
    for blocked in (True, False):
        await _invoke_without_model(child, monkeypatch)
        ctx = AgentCallbackContext(agent=child._react_agent, session=session)
        await manager.execute(
            ctx=ctx, tool_call=ToolCall(id="bash", type="function", name="bash", arguments='{}'),
            session=session, parallel_tool_calls=False,
        )
        assert execute.await_count == (0 if blocked else 1)
        rail._hooks_config.events.clear()
    payload = json.loads(log.read_text())
    assert payload["subagent_type"] == "prebuilt"
    assert payload["session_id"] == "prebuilt-session"


@pytest.mark.asyncio
async def test_replacement_specs_propagate_through_real_invoke(monkeypatch):
    rail = UserHookRail(HooksConfig())
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(rails=[rail]))
    await _invoke_without_model(parent, monkeypatch)
    # Simulate the host replacing the registry between interaction rounds.
    replacement = SubAgentConfig(agent_card=AgentCard(name="replacement"), system_prompt="")
    parent.deep_config.subagents = [replacement]
    await _invoke_without_model(parent, monkeypatch)
    configured = parent.deep_config.subagents[0]
    assert configured is not replacement
    assert len([r for r in configured.rails if rail._owns(r)]) == 1
    await _invoke_without_model(parent, monkeypatch)
    assert parent.deep_config.subagents[0] is configured


@pytest.mark.asyncio
@pytest.mark.parametrize("preconstructed", [False, True])
async def test_replacing_root_rail_retires_stale_child_hooks(tmp_path, monkeypatch, preconstructed):
    """Defend against future host reloads that replace the root rail instance."""
    old_log = tmp_path / "old-hook.jsonl"
    old = UserHookRail(HooksConfig(events={"PreToolUse": [HookMatcher(
        matcher="bash", hooks=[{"command": f"cat >> {shlex.quote(str(old_log))}; exit 2"}],
    )]}))
    custom = UserHookRail(HooksConfig())
    if preconstructed:
        spec = DeepAgent(AgentCard(name="worker")).configure(DeepAgentConfig(rails=[custom]))
        await _invoke_without_model(spec, monkeypatch)
    else:
        spec = SubAgentConfig(agent_card=AgentCard(name="worker"), system_prompt="", rails=[custom])
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(subagents=[spec], rails=[old]))
    await _invoke_without_model(parent, monkeypatch)
    replacement = UserHookRail(HooksConfig())
    # Use SDK reconfiguration and stale-rail cleanup, not a hand-built callback.
    parent.configure(DeepAgentConfig(subagents=parent.deep_config.subagents, rails=[replacement]))
    await _invoke_without_model(parent, monkeypatch)
    configured = parent.deep_config.subagents[0]
    rails = configured.configured_rails() if preconstructed else configured.rails
    assert custom in rails
    assert not any(old._owns(r) for r in rails)
    assert sum(replacement._owns(r) for r in rails) == 1
    child = parent.create_subagent("worker", "replacement-session")
    if not preconstructed:
        for rail in child.configured_rails():
            if isinstance(rail, UserHookRail):
                await child.register_rail(rail)
                child.remove_pending_rail(rail)
    manager = child._react_agent.ability_manager
    execute = AsyncMock(return_value=("executed", None))
    monkeypatch.setattr(manager, "_execute_single_tool_call", execute)
    session = SimpleNamespace(get_session_id=lambda: "replacement-session")
    await manager.execute(
        ctx=AgentCallbackContext(agent=child._react_agent, session=session),
        tool_call=ToolCall(id="bash", type="function", name="bash", arguments='{}'),
        session=session, parallel_tool_calls=False,
    )
    assert execute.await_count == 1
    assert not old_log.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("preconstructed", [False, True])
async def test_keeps_forks_of_other_active_parent_hooks(monkeypatch, preconstructed):
    first, second = UserHookRail(HooksConfig()), UserHookRail(HooksConfig())
    spec = (DeepAgent(AgentCard(name="worker")).configure(DeepAgentConfig()) if preconstructed
            else SubAgentConfig(agent_card=AgentCard(name="worker"), system_prompt=""))
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(subagents=[spec], rails=[first, second]))
    await _invoke_without_model(parent, monkeypatch)
    await _invoke_without_model(parent, monkeypatch)
    configured = parent.deep_config.subagents[0]
    rails = configured.configured_rails() if preconstructed else configured.rails
    assert sum(first._owns(r) for r in rails) == 1
    assert sum(second._owns(r) for r in rails) == 1


@pytest.mark.asyncio
async def test_child_registration_error_aborts_parent_turn(monkeypatch):
    child = DeepAgent(AgentCard(name="worker")).configure(DeepAgentConfig())
    rail = UserHookRail(HooksConfig())
    parent = DeepAgent(AgentCard(name="parent")).configure(DeepAgentConfig(subagents=[child], rails=[rail]))
    monkeypatch.setattr(child, "register_rail", AsyncMock(side_effect=RuntimeError("cannot register hook")))
    with pytest.raises(RuntimeError, match="cannot register hook"):
        await _invoke_without_model(parent, monkeypatch)
    parent._run_single_round_invoke.assert_not_awaited()
    inherited = next(r for r in child.configured_rails() if rail._owns(r))
    assert child.is_pending_rail(inherited)
