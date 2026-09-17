from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.session import kv_cache_product_hooks
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


class _AgentManager:
    def get_agent_nowait(self, _channel_id: str):
        return None


def _session_switch_lifecycle_owner(
    agent_manager: _AgentManager | None = None,
) -> SimpleNamespace:
    """Stub owner with lifecycle helpers bound after prepare/dispatch split."""
    owner = SimpleNamespace(_agent_manager=agent_manager or _AgentManager())
    owner._prepare_session_switch_owner = MethodType(
        AgentWebSocketServer._prepare_session_switch_owner, owner
    )
    owner._dispatch_session_switch_kvc = MethodType(
        AgentWebSocketServer._dispatch_session_switch_kvc, owner
    )
    return owner


class _TeamManager:
    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, str]] = []
        self.prefetch_calls: list[dict[str, str]] = []

    async def prepare_session_switch(
        self,
        session_id: str,
        reason: str = "",
        previous_session_id: str | None = None,
    ) -> None:
        call = {"session_id": session_id, "reason": reason}
        if previous_session_id is not None:
            call["previous_session_id"] = previous_session_id
        self.prepare_calls.append(call)

    async def prefetch_session_kv_cache(
        self,
        session_id: str,
        reason: str = "",
    ) -> bool:
        self.prefetch_calls.append({"session_id": session_id, "reason": reason})
        return True


@pytest.mark.asyncio
async def test_cancel_pending_tasks_cleans_all_kvc_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[str] = []

    async def _record(owner: str) -> None:
        cleanup_calls.append(owner)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "cancel_pending_kv_cache_lifecycle_tasks",
        lambda: _record("root"),
    )
    monkeypatch.setattr(
        "openjiuwen.core.foundation.kv_cache."
        "cancel_pending_session_kv_cache_signals",
        lambda: _record("plan"),
    )
    monkeypatch.setattr(
        "openjiuwen.agent_teams.kv_cache.kv_cache_lifecycle."
        "cancel_pending_signal_tasks",
        lambda: _record("team"),
    )

    await kv_cache_product_hooks.cancel_pending_tasks()

    assert cleanup_calls == ["root", "plan", "team"]


@pytest.mark.asyncio
async def test_disabled_plan_delete_does_not_resolve_live_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoLookupAgentManager:
        def get_agent_nowait(self, _channel_id: str):
            raise AssertionError("disabled affinity must not resolve a Plan agent")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "evict_session_kv_cache",
        lambda **_kwargs: pytest.fail("disabled affinity dispatched evict"),
    )

    assert await kv_cache_product_hooks.evict_plan_session(
        session_id="sess_agent_001",
        agent_manager=_NoLookupAgentManager(),
        channel_id="web",
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("affinity_enabled", [False, True])
async def test_team_switch_context_and_prefetch_for_both_affinity_states(
    monkeypatch: pytest.MonkeyPatch,
    affinity_enabled: bool,
) -> None:
    team_manager = _TeamManager()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: team_manager,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: affinity_enabled,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: True,
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="team_sess_002",
        previous_session_id="team_sess_001",
        params={"mode": "team", "previous_mode": "team"},
    )
    await kv_cache_product_hooks.dispatch_session_switch_signals(
        context=context,
        agent_manager=_AgentManager(),
        channel_id="web",
        team_manager=team_manager,
        target_session_id="team_sess_002",
        previous_session_id="team_sess_001",
        reason="session.switch: ",
    )

    assert context.target_is_team is True
    assert context.previous_is_team is True
    assert context.resolved_mode == "team"
    assert team_manager.prepare_calls == []
    assert team_manager.prefetch_calls == (
        [{"session_id": "team_sess_002", "reason": "session.switch: "}]
        if affinity_enabled
        else []
    )


@pytest.mark.asyncio
async def test_plan_switch_dispatches_root_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offload_calls: list[dict] = []
    prefetch_calls: list[dict] = []
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda _session_id: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "dispatch_offload_session_kv_cache",
        lambda **kwargs: offload_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "dispatch_prefetch_session_kv_cache",
        lambda **kwargs: prefetch_calls.append(kwargs),
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="sess_agent_002",
        previous_session_id="sess_agent_001",
        params={"mode": "agent.plan"},
    )
    await kv_cache_product_hooks.dispatch_session_switch_signals(
        context=context,
        agent_manager=_AgentManager(),
        channel_id="web",
        team_manager=None,
        target_session_id="sess_agent_002",
        previous_session_id="sess_agent_001",
        reason="session.switch: ",
    )

    assert (context.target_is_team, context.resolved_mode) == (
        False,
        "agent.plan",
    )
    assert offload_calls == [
        {
            "session_id": "sess_agent_001",
            "parent_session_id": "sess_agent_001",
            "agent": None,
        }
    ]
    assert prefetch_calls == [
        {
            "session_id": "sess_agent_002",
            "parent_session_id": "sess_agent_002",
            "agent": None,
        }
    ]


@pytest.mark.asyncio
async def test_disabled_plan_switch_skips_kvc_and_preserves_resolved_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda _session_id: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "dispatch_offload_session_kv_cache",
        lambda **_kwargs: pytest.fail("disabled affinity dispatched offload"),
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="sess_agent_002",
        previous_session_id="sess_agent_001",
        params={"mode": "code.normal"},
    )

    assert (context.target_is_team, context.resolved_mode) == (
        False,
        "code.normal",
    )
    assert context.affinity_enabled is False


@pytest.mark.asyncio
async def test_disabled_affinity_keeps_previous_team_fact_for_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda session_id: {
            "mode": "team" if session_id == "team_sess_001" else "agent.plan"
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: False,
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="plan_sess_002",
        previous_session_id="team_sess_001",
        params={"mode": "agent.plan"},
    )

    assert context.affinity_enabled is False
    assert context.target_is_team is False
    assert context.previous_is_team is True
    assert context.resolved_mode == "agent.plan"


@pytest.mark.asyncio
async def test_disabled_affinity_routes_previous_team_to_team_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _TeamManager()
    owner = _session_switch_lifecycle_owner()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: team_manager,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda session_id: {
            "mode": "team" if session_id == "team_sess_001" else "agent.plan"
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: False,
    )

    result = await AgentWebSocketServer._prepare_session_switch_owner(
        owner,
        channel_id="web",
        target_session_id="plan_sess_002",
        previous_session_id="team_sess_001",
        params={"mode": "agent.plan"},
        reason="session.switch: ",
    )

    assert result[:2] == (False, "agent.plan")
    assert team_manager.prepare_calls == [
        {
            "session_id": "plan_sess_002",
            "reason": "session.switch: ",
            "previous_session_id": "team_sess_001",
        }
    ]


@pytest.mark.asyncio
async def test_team_prefetch_does_not_resolve_plan_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _TeamManager()

    class _NoPlanAgentManager:
        def get_agent_nowait(self, _channel_id: str):
            raise AssertionError("Team-only switch must not resolve a Plan agent")

    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: True,
    )

    context = kv_cache_product_hooks.SessionSwitchContext(
        target_is_team=True,
        previous_is_team=True,
        resolved_mode="team",
        affinity_enabled=True,
    )
    await kv_cache_product_hooks.dispatch_session_switch_signals(
        context=context,
        agent_manager=_NoPlanAgentManager(),
        channel_id="web",
        team_manager=team_manager,
        target_session_id="team_sess_002",
        previous_session_id="team_sess_001",
        reason="session.switch: ",
    )

    assert team_manager.prefetch_calls == [
        {"session_id": "team_sess_002", "reason": "session.switch: "}
    ]


@pytest.mark.asyncio
async def test_team_to_plan_routes_previous_session_to_team_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _TeamManager()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: team_manager,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda session_id: {
            "mode": "team" if session_id == "team_sess_001" else "agent.plan"
        },
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_history,
        "history_exists",
        lambda _session_id: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "is_kv_cache_affinity_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle."
        "dispatch_offload_session_kv_cache",
        lambda **_kwargs: pytest.fail("Team owner must handle previous session"),
    )

    context = kv_cache_product_hooks.resolve_session_switch_context(
        target_session_id="plan_sess_002",
        previous_session_id="team_sess_001",
        params={"mode": "agent.plan", "previous_mode": "team"},
    )
    await kv_cache_product_hooks.dispatch_session_switch_signals(
        context=context,
        agent_manager=_AgentManager(),
        channel_id="tui",
        team_manager=team_manager,
        target_session_id="plan_sess_002",
        previous_session_id="team_sess_001",
        reason="session.switch: ",
    )

    assert (context.target_is_team, context.resolved_mode) == (
        False,
        "agent.plan",
    )
    assert context.previous_is_team is True
    assert team_manager.prepare_calls == []


@pytest.mark.asyncio
async def test_team_product_switch_survives_kvc_context_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_manager = _TeamManager()
    owner = _session_switch_lifecycle_owner()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: team_manager,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("kvc unavailable")),
    )

    result = await AgentWebSocketServer._prepare_session_switch_owner(
        owner,
        channel_id="web",
        target_session_id="team_sess_002",
        previous_session_id="team_sess_001",
        params={"mode": "team"},
        reason="session.switch: ",
    )

    assert result[:2] == (True, "team")
    assert team_manager.prepare_calls == [
        {
            "session_id": "team_sess_002",
            "reason": "session.switch: ",
        }
    ]


@pytest.mark.asyncio
async def test_plan_product_switch_preserves_canonical_mode_when_kvc_hook_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _session_switch_lifecycle_owner()
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("kvc unavailable")),
    )

    result = await AgentWebSocketServer._prepare_session_switch_owner(
        owner,
        channel_id="web",
        target_session_id="plan_sess_002",
        previous_session_id="plan_sess_001",
        params={"mode": "code.normal"},
        reason="session.switch: ",
    )

    assert result[:2] == (False, "code.normal")
