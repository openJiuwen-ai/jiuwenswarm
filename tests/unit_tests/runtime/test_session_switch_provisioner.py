# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Direct Runtime tests for transport-neutral ``session.switch`` provisioning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jiuwenswarm.runtime import (
    AgentRuntime,
    RuntimeSessionProvisioner,
    RuntimeStateError,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
    SessionSwitchInput,
    SessionSwitchResult,
)


@dataclass
class _SwitchState:
    events: list[str] = field(default_factory=list)
    context_calls: list[dict[str, Any]] = field(default_factory=list)
    dispatch_calls: list[dict[str, Any]] = field(default_factory=list)
    team_prepare_calls: list[dict[str, Any]] = field(default_factory=list)


class _AgentManager:
    def __init__(self, state: _SwitchState) -> None:
        self._state = state

    async def cancel_all_inflight_work(self, _reason: str) -> None:
        self._state.events.append("runtime.cancel")

    async def cleanup(self) -> None:
        self._state.events.append("runtime.cleanup")


class _PlanController:
    def reset_session(self, _session_id: str) -> None:
        return None


class _TeamManager:
    def __init__(
        self,
        state: _SwitchState,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._state = state
        self._entered = entered
        self._release = release

    async def prepare_session_switch(
        self,
        target_session_id: str,
        reason: str = "",
        previous_session_id: str | None = None,
    ) -> None:
        self._state.events.append("team.prepare")
        self._state.team_prepare_calls.append(
            {
                "target_session_id": target_session_id,
                "previous_session_id": previous_session_id,
                "reason": reason,
            }
        )
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            await self._release.wait()


def _provisioner(state: _SwitchState) -> RuntimeSessionProvisioner:
    return RuntimeSessionProvisioner(
        agent_manager=cast(Any, _AgentManager(state)),
        plan_controller=cast(Any, _PlanController()),
    )


def _runtime(state: _SwitchState) -> AgentRuntime:
    async def initialize() -> None:
        state.events.append("runtime.start")

    return AgentRuntime(
        agent_manager=cast(Any, _AgentManager(state)),
        initializer=initialize,
        plan_controller=cast(Any, _PlanController()),
    )


def _input(
    *,
    channel_id: str = "web",
    target_session_id: str = "target-session",
    previous_session_id: str = "previous-session",
    mode: str = "agent.plan",
    previous_mode: str | None = None,
    team_hint: bool = False,
) -> SessionSwitchInput:
    return SessionSwitchInput(
        channel_id=channel_id,
        target_session_id=target_session_id,
        previous_session_id=previous_session_id,
        mode=mode,
        previous_mode=previous_mode,
        team_hint=team_hint,
    )


def _install_switch_hooks(
    monkeypatch: pytest.MonkeyPatch,
    state: _SwitchState,
    *,
    target_is_team: bool = False,
    previous_is_team: bool = False,
    resolved_mode: str = "agent.plan",
    context_error: BaseException | None = None,
    dispatch_error: BaseException | None = None,
    team_manager: _TeamManager | None = None,
) -> Any:
    from jiuwenswarm.agents.harness import team as team_package
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    context = SimpleNamespace(
        target_is_team=target_is_team,
        previous_is_team=previous_is_team,
        resolved_mode=resolved_mode,
        affinity_enabled=True,
    )
    selected_team_manager = team_manager or _TeamManager(state)

    def resolve_context(**kwargs: Any) -> Any:
        state.events.append("switch.context")
        state.context_calls.append(kwargs)
        if context_error is not None:
            raise context_error
        return context

    async def dispatch_signals(**kwargs: Any) -> None:
        state.events.append("switch.kvc")
        state.dispatch_calls.append(kwargs)
        if dispatch_error is not None:
            raise dispatch_error

    def get_team_manager(channel_id: str) -> _TeamManager:
        state.events.append("team.manager")
        assert channel_id == "web"
        return selected_team_manager

    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        resolve_context,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "dispatch_session_switch_signals",
        dispatch_signals,
    )
    monkeypatch.setattr(team_package, "get_team_manager", get_team_manager)
    return context


@pytest.mark.asyncio
async def test_agent_switch_exposes_result_then_commits_kvc_with_opaque_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SwitchState()
    context = _install_switch_hooks(
        monkeypatch,
        state,
        resolved_mode="code.normal",
    )
    provisioner = _provisioner(state)
    provision_input = _input(
        mode="agent.plan",
        previous_mode="agent.plan",
    )

    prepared = await provisioner.prepare_session_switch(provision_input)

    assert prepared.result == SessionSwitchResult(
        channel_id="web",
        session_id="target-session",
        mode="code.normal",
    )
    assert prepared.state is SessionProvisionState.PREPARED
    assert prepared.commit_timing is (
        SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
    )
    assert state.events == ["switch.context"]
    assert state.context_calls == [
        {
            "target_session_id": "target-session",
            "previous_session_id": "previous-session",
            "params": {
                "mode": "agent.plan",
                "previous_mode": "agent.plan",
                "team": False,
            },
        }
    ]

    result = await provisioner.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        context=SessionProvisionCommitContext(foreground_scope_id="opaque-view-scope"),
    )

    assert result is prepared.result
    assert prepared.state is SessionProvisionState.COMMITTED
    assert state.events == ["switch.context", "switch.kvc"]
    assert state.dispatch_calls == [
        {
            "context": context,
            "channel_id": "web",
            "target_session_id": "target-session",
            "previous_session_id": "previous-session",
            "view_id": "opaque-view-scope",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_is_team", "previous_is_team", "expected_previous"),
    [
        (True, False, None),
        (False, True, "previous-session"),
        (True, True, "previous-session"),
    ],
)
async def test_team_owner_prepare_precedes_commit_kvc(
    monkeypatch: pytest.MonkeyPatch,
    target_is_team: bool,
    previous_is_team: bool,
    expected_previous: str | None,
) -> None:
    state = _SwitchState()
    _install_switch_hooks(
        monkeypatch,
        state,
        target_is_team=target_is_team,
        previous_is_team=previous_is_team,
        resolved_mode="team" if target_is_team else "code.normal",
    )
    provisioner = _provisioner(state)

    prepared = await provisioner.prepare_session_switch(
        _input(
            mode="team" if target_is_team else "code.normal",
            previous_mode="team" if previous_is_team else "agent.plan",
            team_hint=target_is_team,
        )
    )

    assert state.events == ["switch.context", "team.manager", "team.prepare"]
    assert state.team_prepare_calls == [
        {
            "target_session_id": "target-session",
            "previous_session_id": expected_previous,
            "reason": "session.switch: ",
        }
    ]
    assert state.dispatch_calls == []

    await provisioner.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        context=SessionProvisionCommitContext(foreground_scope_id="team-view"),
    )

    assert state.events == [
        "switch.context",
        "team.manager",
        "team.prepare",
        "switch.kvc",
    ]


@pytest.mark.asyncio
async def test_target_metadata_mode_overrides_request_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_package
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    state = _SwitchState()
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "is_kv_cache_affinity_enabled",
        lambda: False,
        raising=False,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks.session_metadata,
        "get_session_metadata",
        lambda session_id: (
            {"mode": "code.normal"} if session_id == "target-session" else {}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "is_kv_cache_affinity_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        team_package,
        "get_team_manager",
        lambda _channel_id: (_ for _ in ()).throw(
            AssertionError("non-Team switch must not resolve TeamManager")
        ),
    )
    provisioner = _provisioner(state)

    prepared = await provisioner.prepare_session_switch(_input(mode="agent.plan"))

    assert prepared.result.mode == "code.normal"
    await provisioner.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        context=SessionProvisionCommitContext(foreground_scope_id="metadata-view"),
    )
    assert prepared.state is SessionProvisionState.COMMITTED


@pytest.mark.asyncio
async def test_context_failure_falls_back_to_input_and_preserves_team_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SwitchState()
    _install_switch_hooks(
        monkeypatch,
        state,
        context_error=RuntimeError("context unavailable"),
    )
    provisioner = _provisioner(state)

    prepared = await provisioner.prepare_session_switch(
        _input(mode="code.normal", previous_mode="team", team_hint=True)
    )

    assert prepared.result == SessionSwitchResult(
        channel_id="web",
        session_id="target-session",
        mode="code.normal",
    )
    assert state.team_prepare_calls == [
        {
            "target_session_id": "target-session",
            "previous_session_id": None,
            "reason": "session.switch: ",
        }
    ]
    await provisioner.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        context=SessionProvisionCommitContext(foreground_scope_id="fallback-view"),
    )
    assert prepared.state is SessionProvisionState.COMMITTED
    assert state.dispatch_calls == []


@pytest.mark.asyncio
async def test_abort_does_not_dispatch_foreground_kvc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SwitchState()
    _install_switch_hooks(monkeypatch, state)
    provisioner = _provisioner(state)
    prepared = await provisioner.prepare_session_switch(_input())

    await provisioner.abort_session_provision(prepared)
    await provisioner.abort_session_provision(prepared)

    assert prepared.state is SessionProvisionState.ABORTED
    assert state.events == ["switch.context"]
    assert state.dispatch_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
async def test_started_switch_commit_attempt_is_terminal_on_hook_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    state = _SwitchState()
    failure: BaseException = (
        asyncio.CancelledError("switch commit cancelled")
        if failure_kind == "cancellation"
        else RuntimeError("switch commit failed")
    )
    _install_switch_hooks(
        monkeypatch,
        state,
        dispatch_error=failure,
    )
    runtime = _runtime(state)

    await runtime.start()
    prepared = await runtime.prepare_session_switch(_input())
    with pytest.raises(type(failure)) as captured:
        await runtime.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
            context=SessionProvisionCommitContext(foreground_scope_id="terminal-view"),
        )

    assert captured.value is failure
    assert prepared.state is SessionProvisionState.COMMITTED
    assert state.events.count("switch.kvc") == 1
    assert len(state.dispatch_calls) == 1
    await runtime.close()
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_missing_target_is_rejected_before_product_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SwitchState()
    _install_switch_hooks(monkeypatch, state)
    provisioner = _provisioner(state)

    with pytest.raises(SessionProvisionError) as captured:
        await provisioner.prepare_session_switch(_input(target_session_id="  "))

    assert str(captured.value) == "session_id is required"
    assert captured.value.code == "BAD_REQUEST"
    assert state.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", ["commit", "abort"])
async def test_agent_runtime_requires_start_and_pending_switch_blocks_close(
    monkeypatch: pytest.MonkeyPatch,
    finalize: str,
) -> None:
    state = _SwitchState()
    _install_switch_hooks(monkeypatch, state)
    runtime = _runtime(state)
    prepared = None

    with pytest.raises(RuntimeStateError, match="runtime is not started"):
        await runtime.prepare_session_switch(_input())

    try:
        await runtime.start()
        prepared = await runtime.prepare_session_switch(_input())

        with pytest.raises(
            RuntimeStateError,
            match="runtime has unfinished session provisions",
        ):
            await asyncio.wait_for(runtime.close(), timeout=1)

        assert runtime.started is True
        assert runtime.closed is False
        assert "runtime.cancel" not in state.events
        assert "runtime.cleanup" not in state.events

        if finalize == "commit":
            await runtime.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
                context=SessionProvisionCommitContext(
                    foreground_scope_id="runtime-view"
                ),
            )
            assert prepared.state is SessionProvisionState.COMMITTED
        else:
            await runtime.abort_session_provision(prepared)
            assert prepared.state is SessionProvisionState.ABORTED

        await runtime.close()
        assert runtime.closed is True
        assert state.events[-2:] == ["runtime.cancel", "runtime.cleanup"]
    finally:
        if not runtime.closed:
            if (
                prepared is not None
                and prepared.state is SessionProvisionState.PREPARED
            ):
                await runtime.abort_session_provision(prepared)
            await runtime.close()

    with pytest.raises(RuntimeStateError, match="runtime is already closed"):
        await runtime.prepare_session_switch(_input())


@pytest.mark.asyncio
async def test_close_fails_fast_while_switch_prepare_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SwitchState()
    prepare_entered = asyncio.Event()
    release_prepare = asyncio.Event()
    team_manager = _TeamManager(
        state,
        entered=prepare_entered,
        release=release_prepare,
    )
    _install_switch_hooks(
        monkeypatch,
        state,
        target_is_team=True,
        resolved_mode="team",
        team_manager=team_manager,
    )
    runtime = _runtime(state)
    prepared = None
    prepare_task: asyncio.Task[Any] | None = None

    try:
        await runtime.start()
        prepare_task = asyncio.create_task(
            runtime.prepare_session_switch(_input(mode="team", team_hint=True))
        )
        await asyncio.wait_for(prepare_entered.wait(), timeout=1)

        with pytest.raises(
            RuntimeStateError,
            match="runtime has unfinished session provisions",
        ):
            await asyncio.wait_for(runtime.close(), timeout=1)

        assert runtime.started is True
        assert runtime.closed is False
        assert "runtime.cancel" not in state.events
        assert "runtime.cleanup" not in state.events

        release_prepare.set()
        prepared = await asyncio.wait_for(prepare_task, timeout=1)
        await runtime.abort_session_provision(prepared)
        await runtime.close()
        assert state.events[-2:] == ["runtime.cancel", "runtime.cleanup"]
    finally:
        release_prepare.set()
        if prepare_task is not None and not prepare_task.done():
            prepared = await prepare_task
        if not runtime.closed:
            if (
                prepared is not None
                and prepared.state is SessionProvisionState.PREPARED
            ):
                await runtime.abort_session_provision(prepared)
            await runtime.close()
