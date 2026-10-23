"""Cron installs ordinary permission rules only for the A4P experiment."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, RejectResult

from jiuwenswarm.agents.harness.common.a4p import runtime as a4p_runtime
from jiuwenswarm.agents.harness.common.a4p.execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    AuthorizationExecutionContext,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.code_rails import build_permission_interrupt
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture
def config(monkeypatch):
    value = {"a4p": {"enabled": True}, "permissions": {"enabled": True, "tools": {"bash": "ask"}}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: value)
    layers = "jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers"
    monkeypatch.setattr(f"{layers}.load_user_permissions", lambda: {})
    monkeypatch.setattr(f"{layers}.load_session_permissions", lambda _sid: {})
    yield value
    AUTHORIZATION_EXECUTION_CONTEXTS.clear()


def _adapter(config):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_cron_execution = True
    adapter._parent_session_id = "cron-test"
    adapter._config_base_cache = config
    return adapter


def _build(config, variant):
    if variant == "single":
        adapter = _adapter(config)
        recipes = adapter._permission_interrupt_rail_infos(config)
        rails = adapter._instantiate_rails(recipes, config)
        assert adapter._permission_rail is None or adapter._permission_rail in rails
        return adapter._permission_rail
    return build_permission_interrupt(
        {"permissions_config": config["permissions"], "model_name": "unused"},
        SwarmBuildContext(
            config=config, mode="team", role="leader", session_id="cron-test",
            channel_id="__cron__" if variant == "team-channel" else "web",
            request_metadata={"cron": {"job_id": "job-1"}} if variant == "team-metadata" else {},
        ),
    )


@pytest.mark.parametrize("variant", ["single", "team-channel", "team-metadata"])
@pytest.mark.parametrize("a4p_enabled,permissions_enabled", [(False, True), (True, False), (True, True)])
def test_cron_rail_registration(config, variant, a4p_enabled, permissions_enabled):
    config["a4p"]["enabled"] = a4p_enabled
    config["permissions"]["enabled"] = permissions_enabled
    assert (_build(config, variant) is not None) == (a4p_enabled and permissions_enabled)


@pytest.mark.parametrize("variant", ["single", "team-channel", "team-metadata"])
def test_cron_smart_approval_does_not_install_a4p_rail(config, variant):
    config["permissions"]["mode"] = "auto"
    assert _build(config, variant) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["single", "team-channel", "team-metadata"])
@pytest.mark.parametrize("rule,token_matches", [("allow", False), ("deny", True), ("ask", True), ("ask", False)])
async def test_assembled_cron_rail_only_asks_a4p_for_ask_rules(config, monkeypatch, variant, rule, token_matches):
    config["permissions"]["tools"]["bash"] = rule
    runtime = SimpleNamespace(
        find_valid_cron_intent_token=AsyncMock(return_value={"tokenId": "ok"} if token_matches else None),
    )
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: runtime)
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(AuthorizationExecutionContext(
        request_id="cron-request", session_id="cron-test", agent_id="agent-1",
        channel_id="web" if variant == "team-metadata" else "__cron__",
        metadata={"cron": {"job_id": "job-1"}},
    ))
    rail = _build(config, variant)
    decision = await rail.resolve_interrupt(
        ctx=SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "cron-test")),
        tool_call=SimpleNamespace(name="bash", arguments={"command": "date"}, id="call-1"),
        user_input=None,
    )
    expected = ApproveResult if rule == "allow" or (rule == "ask" and token_matches) else RejectResult
    assert isinstance(decision, expected)
    assert runtime.find_valid_cron_intent_token.await_count == (1 if rule == "ask" else 0)


@pytest.mark.asyncio
async def test_cron_permission_reload_creates_updates_registers_and_removes_rail(config):
    adapter = _adapter(config)
    adapter._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock())
    adapter._update_permission_rail(config)
    rail = adapter._permission_rail
    assert rail is not None
    await adapter._ensure_permission_rail_live_registered()
    adapter._instance.register_rail.assert_awaited_once_with(rail)

    config["permissions"]["tools"]["bash"] = "deny"
    adapter._update_permission_rail(config)
    assert adapter._permission_rail is rail
    result = await rail._engine.check_permission(tool_name="bash", tool_args={"command": "date"})
    assert result.permission.value == "deny"

    config["a4p"]["enabled"] = False
    adapter._update_permission_rail(config)
    assert adapter._permission_rail is None
    await adapter._unregister_permission_rail_if_disabled(config, detached_rail=rail)
    adapter._instance.unregister_rail.assert_awaited_once_with(rail)
    await adapter._ensure_permission_rail_live_registered()
    assert adapter._instance.register_rail.await_count == 1


@pytest.mark.asyncio
async def test_cron_permissions_switch_off_unregisters_live_rail(config):
    adapter = _adapter(config)
    adapter._update_permission_rail(config)
    rail = adapter._permission_rail
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    config["permissions"]["enabled"] = False
    adapter._update_permission_rail(config)
    await adapter._unregister_permission_rail_if_disabled(config, detached_rail=rail)
    assert adapter._permission_rail is None
    adapter._instance.unregister_rail.assert_awaited_once_with(rail)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing-job", "verification-error"])
@pytest.mark.parametrize("channel", ["__cron__", "web"])
async def test_assembled_cron_ask_failure_never_interrupts(config, monkeypatch, failure, channel):
    runtime = SimpleNamespace(find_valid_cron_intent_token=AsyncMock(side_effect=RuntimeError("verification failed")))
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: runtime)
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(AuthorizationExecutionContext(
        request_id="cron-request", session_id="cron-test", agent_id="agent-1", channel_id=channel,
        metadata={"cron": {"job_id": "job-1"} if failure != "missing-job" else {"run_id": "run-1"}},
    ))
    rail = _build(config, "single" if channel == "__cron__" else "team-metadata")
    decision = await rail.resolve_interrupt(
        ctx=SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "cron-test")),
        tool_call=SimpleNamespace(name="bash", arguments={"command": "date"}, id="call-1"),
        user_input=None,
    )
    assert isinstance(decision, RejectResult)
    assert "A4P_CRON_SCOPE_DENIED" in str(decision.tool_result)


@pytest.mark.asyncio
async def test_cron_rail_reaches_live_agent_after_configure(config):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
    from openjiuwen.harness import DeepAgent, DeepAgentConfig

    adapter = _adapter(config)
    adapter._update_permission_rail(config)
    rail = adapter._permission_rail
    agent = DeepAgent(AgentCard(name="cron-a4p-live-registration"))
    agent.configure(DeepAgentConfig(rails=[], auto_create_workspace=False))
    await agent.ensure_initialized()
    try:
        agent.configure(DeepAgentConfig(rails=[rail], auto_create_workspace=False))
        assert rail in agent._pending_rails
        adapter._instance = agent
        await adapter._ensure_permission_rail_live_registered()
        assert rail in agent._registered_rails
        event = agent._react_agent._agent_callback_manager._get_agent_event(AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert Runner.callback_framework.list_callbacks(event)
    finally:
        await agent._agent_callback_manager.clear()
