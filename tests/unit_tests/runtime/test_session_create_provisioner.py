# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Direct Runtime tests for transport-neutral ``session.create`` provisioning."""

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
    SessionCreateInput,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
)


@dataclass
class _State:
    events: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)


class _Manager:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def claim_prewarmed_session(self, **kwargs: Any) -> Any:
        self.state.events.append("claim")
        return SimpleNamespace(
            session_id="created-session",
            prewarm_hit=True,
            prewarm_status="ready",
        )

    def activate_session_prewarm(self, session_id: str) -> None:
        self.state.events.append(f"activate:{session_id}")

    async def release_session_prewarm_claim(self, session_id: str) -> None:
        self.state.events.append(f"release:{session_id}")
        self.state.released.append(session_id)

    async def cancel_all_inflight_work(self, _reason: str) -> None:
        self.state.events.append("runtime.cancel")

    async def cleanup(self) -> None:
        self.state.events.append("runtime.cleanup")


class _PlanController:
    def reset_session(self, _session_id: str) -> None:
        return None


def _provisioner(state: _State) -> RuntimeSessionProvisioner:
    return RuntimeSessionProvisioner(
        agent_manager=cast(Any, _Manager(state)),
        plan_controller=cast(Any, _PlanController()),
    )


def _runtime(state: _State) -> AgentRuntime:
    async def initialize() -> None:
        state.events.append("runtime.start")

    return AgentRuntime(
        agent_manager=cast(Any, _Manager(state)),
        initializer=initialize,
        plan_controller=cast(Any, _PlanController()),
    )


def _input(**overrides: Any) -> SessionCreateInput:
    values = {
        "channel_id": "web",
        "create_token": "token",
        "mode": "agent",
        "work_mode": "work",
        "work_mode_explicit": False,
    }
    values.update(overrides)
    return SessionCreateInput(**values)


def _install_product_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    state: _State,
    *,
    target_is_team: bool = False,
    dispatch_error: BaseException | None = None,
) -> None:
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_metadata
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(
        session_metadata,
        "get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        RuntimeSessionProvisioner,
        "_is_prewarm_model_eligible",
        staticmethod(lambda _model: True),
    )

    context = SimpleNamespace(
        target_is_team=target_is_team,
        previous_is_team=False,
    )

    def resolve_context(**_kwargs: Any) -> Any:
        state.events.append("kvc.context")
        return context

    async def dispatch(**_kwargs: Any) -> None:
        state.events.append("kvc.dispatch")
        if dispatch_error is not None:
            raise dispatch_error

    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        resolve_context,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "dispatch_session_switch_signals",
        dispatch,
    )


@pytest.mark.asyncio
async def test_create_prepares_metadata_and_commits_kvc_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    provisioner = _provisioner(state)

    prepared = await provisioner.prepare_session_create(_input())

    assert prepared.commit_timing is SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY
    assert prepared.state is SessionProvisionState.PREPARED
    assert prepared.result.session_id == "created-session"
    assert prepared.result.created is True
    assert (tmp_path / "created-session" / "metadata.json").is_file()
    assert state.events == [
        "claim",
        "activate:created-session",
        "kvc.context",
    ]

    await provisioner.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
        context=SessionProvisionCommitContext(foreground_scope_id="opaque-view"),
    )
    await asyncio.sleep(0)

    assert prepared.state is SessionProvisionState.COMMITTED
    assert state.events[-1] == "kvc.dispatch"
    assert state.released == []


@pytest.mark.asyncio
async def test_team_prepare_finishes_before_create_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.agents.harness import team as team_package

    state = _State()
    _install_product_hooks(
        monkeypatch,
        tmp_path,
        state,
        target_is_team=True,
    )

    class TeamManager:
        async def prepare_session_switch(self, *_args: Any, **_kwargs: Any) -> None:
            state.events.append("team.prepare")

    monkeypatch.setattr(
        team_package, "get_team_manager", lambda _channel: TeamManager()
    )

    prepared = await _provisioner(state).prepare_session_create(
        _input(mode="team", team_hint=True)
    )

    assert state.events[-2:] == ["kvc.context", "team.prepare"]
    assert prepared.state is SessionProvisionState.PREPARED


@pytest.mark.asyncio
async def test_team_hint_triggers_prepare_when_mode_is_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.agents.harness import team as team_package
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)

    class TeamManager:
        async def prepare_session_switch(self, *_args: Any, **_kwargs: Any) -> None:
            state.events.append("team.prepare")

    monkeypatch.setattr(
        team_package, "get_team_manager", lambda _channel: TeamManager()
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no context")),
    )

    prepared = await _provisioner(state).prepare_session_create(
        _input(mode="agent", team_hint=True)
    )

    assert "team.prepare" in state.events
    assert prepared.result.canonical_mode == "agent"


@pytest.mark.asyncio
async def test_successful_context_team_flag_overrides_request_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.agents.harness import team as team_package

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    monkeypatch.setattr(
        team_package,
        "get_team_manager",
        lambda _channel: (_ for _ in ()).throw(
            AssertionError("authoritative non-Team context must skip Team prepare")
        ),
    )

    prepared = await _provisioner(state).prepare_session_create(
        _input(mode="agent", team_hint=True)
    )

    assert prepared.state is SessionProvisionState.PREPARED
    assert "team.prepare" not in state.events


@pytest.mark.asyncio
async def test_unavailable_context_does_not_infer_previous_team_from_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.agents.harness import team as team_package
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no context")),
    )
    monkeypatch.setattr(
        team_package,
        "get_team_manager",
        lambda _channel: (_ for _ in ()).throw(
            AssertionError("legacy fallback does not infer previous Team")
        ),
    )

    provisioner = _provisioner(state)
    prepared = await provisioner.prepare_session_create(_input(previous_mode="team"))

    assert prepared.state is SessionProvisionState.PREPARED
    await provisioner.abort_session_provision(prepared)


@pytest.mark.asyncio
async def test_explicit_new_tui_id_always_runs_project_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.server.runtime.session import project_store

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    calls: list[dict[str, Any]] = []

    def find_project(params: dict[str, Any]) -> None:
        calls.append(params.copy())
        return None

    monkeypatch.setattr(
        project_store,
        "find_or_create_code_project_for_tui_params",
        find_project,
    )

    with pytest.raises(SessionProvisionError):
        await _provisioner(state).prepare_session_create(
            _input(
                channel_id="tui",
                requested_session_id="explicit-project",
                create_token="",
                project_id="already-supplied",
                project_dir=str(tmp_path),
                cwd=str(tmp_path),
                work_mode="code",
            )
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_non_explicit_tui_with_project_id_skips_project_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.server.runtime.session import project_store

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    monkeypatch.setattr(
        project_store,
        "find_or_create_code_project_for_tui_params",
        lambda _params: (_ for _ in ()).throw(
            AssertionError("existing project_id must skip project creation")
        ),
    )
    monkeypatch.setattr(
        project_store,
        "resolve_session_project_binding",
        lambda project_id, project_dir: (project_id, project_dir, None, None),
    )
    monkeypatch.setattr(
        project_store,
        "get_project_by_id",
        lambda _project_id, cache_bust=True: SimpleNamespace(work_mode="code"),
    )

    prepared = await _provisioner(state).prepare_session_create(
        _input(
            channel_id="tui",
            project_id="existing-project",
            project_dir=str(tmp_path),
            cwd=str(tmp_path),
            work_mode="code",
        )
    )

    assert prepared.result.project_id == "existing-project"


def test_projectless_workspace_only_accepts_path_values_as_directories() -> None:
    class StringLike:
        def __str__(self) -> str:
            return "D:/not-a-real-input-type"

    assert RuntimeSessionProvisioner._uses_projectless_workspace(
        {"mode": "agent", "project_dir": StringLike()},
        "tui",
    )
    assert not RuntimeSessionProvisioner._uses_projectless_workspace(
        {"mode": "agent", "project_dir": "D:/workspace"},
        "tui",
    )


@pytest.mark.asyncio
async def test_abort_before_delivery_releases_claim_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    provisioner = _provisioner(state)
    prepared = await provisioner.prepare_session_create(_input())

    await provisioner.abort_session_provision(prepared)
    await provisioner.abort_session_provision(prepared)

    assert prepared.state is SessionProvisionState.ABORTED
    assert state.released == ["created-session"]
    assert "kvc.dispatch" not in state.events


@pytest.mark.asyncio
async def test_after_delivery_commit_does_not_wait_for_slow_kvc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_product_hooks

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_dispatch(**_kwargs: Any) -> None:
        state.events.append("kvc.dispatch")
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        kv_cache_product_hooks,
        "dispatch_session_switch_signals",
        slow_dispatch,
    )
    provisioner = _provisioner(state)
    prepared = await provisioner.prepare_session_create(_input())

    await asyncio.wait_for(
        provisioner.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
        ),
        timeout=0.2,
    )

    assert prepared.state is SessionProvisionState.COMMITTED
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    await asyncio.wait_for(provisioner.close_background_tasks(), timeout=0.2)
    assert not provisioner._background_create_kvc_tasks


@pytest.mark.asyncio
async def test_after_delivery_kvc_failure_is_logged_and_drained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.runtime import session_provisioner as provisioner_module

    state = _State()
    failure = RuntimeError("kvc failed")
    warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        provisioner_module.logger,
        "warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )
    _install_product_hooks(
        monkeypatch,
        tmp_path,
        state,
        dispatch_error=failure,
    )
    provisioner = _provisioner(state)
    prepared = await provisioner.prepare_session_create(_input())

    await provisioner.commit_session_provision(
        prepared,
        timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
    )
    for _ in range(3):
        await asyncio.sleep(0)

    assert prepared.state is SessionProvisionState.COMMITTED
    assert not provisioner._background_create_kvc_tasks
    assert len(warnings) == 1
    assert warnings[0][0][:2] == (
        "Session create KVC dispatch failed after result delivery: %s",
        failure,
    )
    assert warnings[0][1]["exc_info"][1] is failure
    await provisioner.close_background_tasks()


@pytest.mark.asyncio
async def test_prepare_failure_compensates_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)

    def fail_metadata(**_kwargs: Any) -> None:
        raise RuntimeError("metadata failed")

    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_metadata, "init_session_metadata", fail_metadata)

    with pytest.raises(RuntimeError, match="metadata failed"):
        await _provisioner(state).prepare_session_create(_input())

    assert state.released == ["created-session"]


@pytest.mark.asyncio
async def test_prepare_cancellation_preserves_same_object_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    cancellation = asyncio.CancelledError("create cancelled")

    from jiuwenswarm.server.runtime.session import session_metadata

    def cancel_metadata(**_kwargs: Any) -> None:
        raise cancellation

    monkeypatch.setattr(session_metadata, "init_session_metadata", cancel_metadata)

    with pytest.raises(asyncio.CancelledError) as captured:
        await _provisioner(state).prepare_session_create(_input())

    assert captured.value is cancellation
    assert state.released == ["created-session"]


@pytest.mark.asyncio
async def test_compensation_error_does_not_mask_prepare_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    primary = RuntimeError("metadata failed")

    from jiuwenswarm.server.runtime.session import session_metadata

    def fail_metadata(**_kwargs: Any) -> None:
        raise primary

    async def fail_release(_session_id: str) -> None:
        raise RuntimeError("claim cleanup failed")

    manager = _Manager(state)
    monkeypatch.setattr(session_metadata, "init_session_metadata", fail_metadata)
    monkeypatch.setattr(manager, "release_session_prewarm_claim", fail_release)
    provisioner = RuntimeSessionProvisioner(
        agent_manager=cast(Any, manager),
        plan_controller=cast(Any, _PlanController()),
    )

    with pytest.raises(RuntimeError) as captured:
        await provisioner.prepare_session_create(_input())

    assert captured.value is primary


@pytest.mark.asyncio
async def test_explicit_id_prepare_failure_preserves_error_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    primary = RuntimeError("owner prepare failed")
    provisioner = _provisioner(state)

    async def fail_owner(**_kwargs: Any) -> Any:
        raise primary

    monkeypatch.setattr(provisioner, "_prepare_create_owner", fail_owner)

    with pytest.raises(RuntimeError) as captured:
        await provisioner.prepare_session_create(
            _input(
                channel_id="tui",
                requested_session_id="explicit-failure",
                create_token="",
                work_mode="code",
            )
        )

    assert captured.value is primary
    lock = provisioner._external_create_locks.get("explicit-failure")
    assert lock is None or not lock.locked()


@pytest.mark.asyncio
async def test_tui_explicit_id_bypasses_claim_and_lock_releases_on_abort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    provisioner = _provisioner(state)
    explicit = _input(
        channel_id="tui",
        requested_session_id="explicit-session",
        create_token="",
        work_mode="code",
    )

    prepared = await provisioner.prepare_session_create(explicit)
    lock = provisioner._external_create_locks["explicit-session"]
    assert lock.locked()
    assert prepared.result.explicit_id_compatibility is True
    assert prepared.result.prewarm_status == "bypassed"
    assert "claim" not in state.events

    await provisioner.abort_session_provision(prepared)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_explicit_tui_id_is_serialized_across_runtime_instances(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    first = _provisioner(state)
    second = _provisioner(state)
    explicit = _input(
        channel_id="tui",
        requested_session_id="shared-explicit-session",
        create_token="",
        work_mode="code",
    )

    first_prepared = await first.prepare_session_create(explicit)
    second_prepare = asyncio.create_task(second.prepare_session_create(explicit))
    await asyncio.sleep(0)
    assert not second_prepare.done()

    await first.abort_session_provision(first_prepared)
    second_prepared = await asyncio.wait_for(second_prepare, timeout=0.2)
    assert second_prepared.result.created is False
    await second.abort_session_provision(second_prepared)


@pytest.mark.asyncio
async def test_runtime_create_barrier_and_pending_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    runtime = _runtime(state)

    with pytest.raises(RuntimeStateError, match="runtime is not started"):
        await runtime.prepare_session_create(_input())

    await runtime.start()
    prepared = await runtime.prepare_session_create(_input())
    with pytest.raises(
        RuntimeStateError,
        match="runtime has unfinished session provisions",
    ):
        await runtime.close()

    await runtime.abort_session_provision(prepared)
    await runtime.close()
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_non_tui_explicit_id_and_missing_token_keep_legacy_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    provisioner = _provisioner(state)

    with pytest.raises(SessionProvisionError) as explicit_error:
        await provisioner.prepare_session_create(
            _input(requested_session_id="not-allowed")
        )
    assert explicit_error.value.code is None

    with pytest.raises(
        SessionProvisionError, match="create_token is required"
    ) as token:
        await provisioner.prepare_session_create(_input(create_token=""))
    assert token.value.code is None


@pytest.mark.asyncio
async def test_project_work_mode_mismatch_keeps_exact_legacy_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from jiuwenswarm.server.runtime.session import project_store

    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    monkeypatch.setattr(
        project_store,
        "resolve_session_project_binding",
        lambda _project_id, _project_dir: (
            "project-1",
            str(tmp_path),
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        project_store,
        "get_project_by_id",
        lambda _project_id, *, cache_bust: SimpleNamespace(work_mode="code"),
    )

    with pytest.raises(SessionProvisionError) as captured:
        await _provisioner(state).prepare_session_create(
            _input(
                project_id="project-1",
                project_dir=str(tmp_path),
                work_mode="work",
                work_mode_explicit=True,
            )
        )

    assert captured.value.code == "BAD_REQUEST"
    assert str(captured.value) == (
        "work_mode mismatch: project is 'code'"
        f"{' ' * 37}"
        "but request specified 'work'"
    )
