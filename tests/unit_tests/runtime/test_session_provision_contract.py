# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any, cast

import pytest

from jiuwenswarm.runtime import (
    PreparedSessionProvision,
    RuntimeSessionProvisioner,
    SessionCreateInput,
    SessionCreateResult,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
    SessionProvisionStateError,
    SessionProvisionerContract,
    SessionSwitchInput,
    SessionSwitchResult,
)

PROJECT_ROOT = Path(__file__).parents[3]
_FORBIDDEN_CONTRACT_FIELDS = {
    "agent_request",
    "agent_response",
    "metadata",
    "params",
    "request_id",
    "send_lock",
    "view_id",
    "wire",
    "ws",
}


class _AgentManager:
    pass


class _PlanController:
    def reset_session(self, _session_id: str) -> None:
        return None


def _provisioner() -> RuntimeSessionProvisioner:
    return RuntimeSessionProvisioner(
        agent_manager=cast(Any, _AgentManager()),
        plan_controller=cast(Any, _PlanController()),
    )


def _fork_result() -> SessionForkResult:
    return SessionForkResult(
        channel_id="tui",
        source_session_id="source-session",
        session_id="target-session",
        title="Branch",
    )


def _stage(
    provisioner: RuntimeSessionProvisioner,
    *,
    timing: SessionProvisionCommitTiming = (
        SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
    ),
    commit_hook: (
        Callable[[SessionProvisionCommitContext], Awaitable[None]] | None
    ) = None,
    abort_hook: Callable[[], Awaitable[None]] | None = None,
) -> PreparedSessionProvision[SessionForkResult]:
    return provisioner._stage_session_provision(
        _fork_result(),
        commit_timing=timing,
        commit_hook=commit_hook,
        abort_hook=abort_hook,
    )


async def _commit(
    provisioner: RuntimeSessionProvisioner,
    prepared: PreparedSessionProvision[SessionForkResult],
    *,
    timing: SessionProvisionCommitTiming | None = None,
    context: SessionProvisionCommitContext | None = None,
) -> SessionForkResult:
    return await provisioner.commit_session_provision(
        prepared,
        timing=timing or prepared.commit_timing,
        context=context,
    )


def test_session_provision_contract_cold_import_has_no_transport_modules() -> None:
    script = """
import sys
from jiuwenswarm.runtime import (
    PreparedSessionProvision,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionerContract,
    SessionCreateInput,
    SessionCreateResult,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionState,
    SessionSwitchInput,
    SessionSwitchResult,
)

assert PreparedSessionProvision
assert SessionProvisionCommitContext and SessionProvisionCommitTiming
assert SessionProvisionerContract
assert SessionCreateInput and SessionCreateResult
assert SessionSwitchInput and SessionSwitchResult
assert SessionForkInput and SessionForkResult
assert SessionProvisionState
unexpected = sorted(
    name for name in sys.modules
    if name.startswith("jiuwenswarm.gateway")
    or name.startswith("jiuwenswarm.server.agent_ws_server")
    or name.startswith("jiuwenswarm.common.e2a.wire_codec")
    or name == "websockets"
)
print("TRANSPORT_MODULES=" + repr(unexpected))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "TRANSPORT_MODULES=[]" in result.stdout


def test_inputs_and_results_are_frozen_transport_neutral_domain_values() -> None:
    create_input = SessionCreateInput(
        channel_id="tui",
        requested_session_id="external-session",
        previous_session_id="previous-session",
        create_token="create-token",
        persist_session=True,
        persist_session_supplied=True,
        mode="code.normal",
        previous_mode="agent.plan",
        project_id="project-id",
        project_dir="D:/project",
        cwd="D:/project",
        work_mode="code",
        work_mode_explicit=True,
        title="Session",
        user_id="user-id",
        model_name="model-name",
        cron_id="cron-id",
    )
    switch_input = SessionSwitchInput(
        channel_id="web",
        target_session_id="target-session",
        previous_session_id="previous-session",
        mode="team",
        previous_mode="agent.plan",
        team_hint=True,
    )
    fork_input = SessionForkInput(
        channel_id="tui",
        source_session_id="source-session",
        target_session_id=None,
        title="Branch",
    )
    create_result = SessionCreateResult(
        channel_id="tui",
        session_id="external-session",
        project_id="project-id",
        project_dir="D:/project",
        work_mode="code",
        persist_session=True,
        prewarm_hit=False,
        prewarm_status="bypassed",
        created=True,
        canonical_mode="code.normal",
        explicit_id_compatibility=True,
    )
    switch_result = SessionSwitchResult(
        channel_id="web",
        session_id="target-session",
        mode="team",
    )
    fork_result = _fork_result()

    values = (
        create_input,
        switch_input,
        fork_input,
        create_result,
        switch_result,
        fork_result,
    )
    for value in values:
        field_names = {field.name for field in fields(value)}
        assert field_names.isdisjoint(_FORBIDDEN_CONTRACT_FIELDS)
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            value.channel_id = "changed"  # type: ignore[misc]

    assert create_input.persist_session_supplied is True
    assert create_input.work_mode_explicit is True
    assert create_result.explicit_id_compatibility is True
    assert switch_result.switched is True


@pytest.mark.parametrize("invalid", [1, "true", None])
def test_create_input_rejects_non_boolean_persist_session(invalid: object) -> None:
    with pytest.raises(TypeError, match="persist_session must be a boolean"):
        SessionCreateInput(
            channel_id="tui",
            persist_session=invalid,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("invalid", [1, "true"])
def test_result_flags_reject_non_boolean_values(invalid: object) -> None:
    with pytest.raises(TypeError, match="switched must be a boolean"):
        SessionSwitchResult(
            channel_id="web",
            session_id="session-id",
            mode="agent.plan",
            switched=invalid,  # type: ignore[arg-type]
        )


def test_session_provision_error_preserves_optional_legacy_code() -> None:
    uncoded = SessionProvisionError("create_token is required")
    coded = SessionProvisionError("source session not found", code="NOT_FOUND")

    assert str(uncoded) == "create_token is required"
    assert uncoded.message == "create_token is required"
    assert uncoded.code is None
    assert str(coded) == "source session not found"
    assert coded.code == "NOT_FOUND"


def test_public_contract_declares_transport_neutral_two_phase_surface() -> None:
    methods = {
        "prepare_session_create",
        "prepare_session_switch",
        "prepare_session_fork",
        "commit_session_provision",
        "abort_session_provision",
    }
    context = SessionProvisionCommitContext(foreground_scope_id="opaque-scope")

    assert methods.issubset(dir(SessionProvisionerContract))
    assert {field.name for field in fields(context)} == {"foreground_scope_id"}
    assert context.foreground_scope_id == "opaque-scope"
    assert not hasattr(context, "view_id")
    assert not hasattr(context, "__dict__")
    with pytest.raises(FrozenInstanceError):
        context.foreground_scope_id = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_prepare_exposes_result_without_running_either_finalizer() -> None:
    provisioner = _provisioner()
    events: list[tuple[str, str | None]] = []

    async def commit(context: SessionProvisionCommitContext) -> None:
        events.append(("commit", context.foreground_scope_id))

    async def abort() -> None:
        events.append(("abort", None))

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )

    assert isinstance(prepared, PreparedSessionProvision)
    assert prepared.state is SessionProvisionState.PREPARED
    assert prepared.commit_timing is (
        SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
    )
    assert events == []


@pytest.mark.asyncio
async def test_commit_is_idempotent_and_forwards_opaque_context_once() -> None:
    provisioner = _provisioner()
    contexts: list[SessionProvisionCommitContext] = []

    async def commit(context: SessionProvisionCommitContext) -> None:
        contexts.append(context)

    prepared = _stage(
        provisioner,
        timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
        commit_hook=commit,
    )
    first_context = SessionProvisionCommitContext(foreground_scope_id="opaque-scope")
    second_context = SessionProvisionCommitContext(
        foreground_scope_id="ignored-on-repeat"
    )

    first = await _commit(provisioner, prepared, context=first_context)
    second = await _commit(provisioner, prepared, context=second_context)

    assert first is prepared.result
    assert second is prepared.result
    assert contexts == [first_context]
    assert prepared.state is SessionProvisionState.COMMITTED


@pytest.mark.asyncio
async def test_commit_rejects_wrong_delivery_timing_without_side_effect() -> None:
    provisioner = _provisioner()
    calls = 0

    async def commit(_context: SessionProvisionCommitContext) -> None:
        nonlocal calls
        calls += 1

    prepared = _stage(
        provisioner,
        timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
        commit_hook=commit,
    )

    with pytest.raises(
        SessionProvisionStateError,
        match="session provision commit timing mismatch",
    ):
        await _commit(
            provisioner,
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )

    assert calls == 0
    assert prepared.state is SessionProvisionState.PREPARED
    await _commit(provisioner, prepared)
    assert calls == 1


@pytest.mark.asyncio
async def test_commit_without_context_supplies_empty_opaque_context() -> None:
    provisioner = _provisioner()
    contexts: list[SessionProvisionCommitContext] = []

    async def commit(context: SessionProvisionCommitContext) -> None:
        contexts.append(context)

    prepared = _stage(provisioner, commit_hook=commit)
    await _commit(provisioner, prepared)

    assert len(contexts) == 1
    assert contexts[0].foreground_scope_id is None


@pytest.mark.asyncio
async def test_abort_is_idempotent_and_runs_compensation_once() -> None:
    provisioner = _provisioner()
    abort_count = 0

    async def abort() -> None:
        nonlocal abort_count
        abort_count += 1

    prepared = _stage(provisioner, abort_hook=abort)

    await provisioner.abort_session_provision(prepared)
    await provisioner.abort_session_provision(prepared)

    assert abort_count == 1
    assert prepared.state is SessionProvisionState.ABORTED


@pytest.mark.asyncio
async def test_conflicting_terminal_decisions_are_rejected() -> None:
    provisioner = _provisioner()
    committed = _stage(provisioner)
    aborted = _stage(provisioner)

    await _commit(provisioner, committed)
    with pytest.raises(
        SessionProvisionStateError,
        match="cannot abort a committed session provision",
    ):
        await provisioner.abort_session_provision(committed)

    await provisioner.abort_session_provision(aborted)
    with pytest.raises(
        SessionProvisionStateError,
        match="cannot commit an aborted session provision",
    ):
        await _commit(provisioner, aborted)


@pytest.mark.asyncio
async def test_prepared_operation_cannot_be_finalized_by_another_provisioner() -> None:
    owner = _provisioner()
    foreign = _provisioner()
    prepared = _stage(owner)

    with pytest.raises(
        SessionProvisionStateError,
        match="belongs to a different provisioner",
    ):
        await _commit(foreign, prepared)
    with pytest.raises(
        SessionProvisionStateError,
        match="belongs to a different provisioner",
    ):
        await foreign.abort_session_provision(prepared)

    assert prepared.state is SessionProvisionState.PREPARED


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["commit", "abort"])
async def test_finalizer_exception_keeps_direction_retryable(
    decision: str,
) -> None:
    provisioner = _provisioner()
    attempts = 0
    failure = RuntimeError(f"{decision} failed")

    async def commit(_context: SessionProvisionCommitContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure

    async def abort() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )

    async def finalize() -> object:
        if decision == "commit":
            return await _commit(provisioner, prepared)
        return await provisioner.abort_session_provision(prepared)

    with pytest.raises(RuntimeError) as captured:
        await finalize()
    assert captured.value is failure
    assert prepared.state is (
        SessionProvisionState.COMMITTING
        if decision == "commit"
        else SessionProvisionState.ABORTING
    )

    await finalize()
    assert attempts == 2
    assert prepared.state is (
        SessionProvisionState.COMMITTED
        if decision == "commit"
        else SessionProvisionState.ABORTED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_decision", ["commit", "abort"])
async def test_failed_finalizer_pins_decision_and_rejects_opposite(
    failed_decision: str,
) -> None:
    provisioner = _provisioner()
    events: list[str] = []

    async def commit(_context: SessionProvisionCommitContext) -> None:
        events.append("commit")
        raise RuntimeError("commit failed")

    async def abort() -> None:
        events.append("abort")
        raise RuntimeError("abort failed")

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )

    with pytest.raises(RuntimeError, match=f"{failed_decision} failed"):
        if failed_decision == "commit":
            await _commit(provisioner, prepared)
        else:
            await provisioner.abort_session_provision(prepared)

    with pytest.raises(
        SessionProvisionStateError,
        match=f"cannot .* after {failed_decision} finalization started",
    ):
        if failed_decision == "commit":
            await provisioner.abort_session_provision(prepared)
        else:
            await _commit(provisioner, prepared)

    assert events == [failed_decision]
    assert prepared.state is (
        SessionProvisionState.COMMITTING
        if failed_decision == "commit"
        else SessionProvisionState.ABORTING
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
async def test_failed_commit_pins_context_for_retry(failure_kind: str) -> None:
    provisioner = _provisioner()
    contexts: list[SessionProvisionCommitContext] = []

    async def commit(context: SessionProvisionCommitContext) -> None:
        contexts.append(context)
        if len(contexts) == 1:
            if failure_kind == "cancellation":
                raise asyncio.CancelledError("commit cancelled")
            raise RuntimeError("commit failed")

    prepared = _stage(
        provisioner,
        timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
        commit_hook=commit,
    )
    first_context = SessionProvisionCommitContext(foreground_scope_id="scope-a")
    changed_context = SessionProvisionCommitContext(foreground_scope_id="scope-b")

    if failure_kind == "cancellation":
        with pytest.raises(asyncio.CancelledError, match="commit cancelled"):
            await _commit(provisioner, prepared, context=first_context)
    else:
        with pytest.raises(RuntimeError, match="commit failed"):
            await _commit(provisioner, prepared, context=first_context)
    assert prepared.state is SessionProvisionState.COMMITTING

    with pytest.raises(
        SessionProvisionStateError,
        match="commit context does not match first attempt",
    ):
        await _commit(provisioner, prepared, context=changed_context)
    assert contexts == [first_context]

    await _commit(provisioner, prepared, context=first_context)
    assert contexts == [first_context, first_context]
    assert prepared.state is SessionProvisionState.COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["commit", "abort"])
async def test_finalizer_cancellation_propagates_and_remains_retryable(
    decision: str,
) -> None:
    provisioner = _provisioner()
    attempts = 0

    async def commit(_context: SessionProvisionCommitContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError(f"{decision} cancelled")

    async def abort() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError(f"{decision} cancelled")

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )

    async def finalize() -> object:
        if decision == "commit":
            return await _commit(provisioner, prepared)
        return await provisioner.abort_session_provision(prepared)

    with pytest.raises(asyncio.CancelledError, match=f"{decision} cancelled"):
        await finalize()
    assert prepared.state is (
        SessionProvisionState.COMMITTING
        if decision == "commit"
        else SessionProvisionState.ABORTING
    )

    await finalize()
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["commit", "abort"])
async def test_concurrent_identical_finalization_runs_hook_once(decision: str) -> None:
    provisioner = _provisioner()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def commit(_context: SessionProvisionCommitContext) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    async def abort() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )

    async def finalize() -> object:
        if decision == "commit":
            return await _commit(provisioner, prepared)
        return await provisioner.abort_session_provision(prepared)

    first = asyncio.create_task(finalize())
    second = asyncio.create_task(finalize())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        outcomes = await asyncio.gather(first, second)
    finally:
        release.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    assert calls == 1
    if decision == "commit":
        assert outcomes == [prepared.result, prepared.result]
    else:
        assert outcomes == [None, None]


@pytest.mark.asyncio
async def test_commit_wins_race_and_conflicting_abort_has_no_side_effect() -> None:
    provisioner = _provisioner()
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    events: list[str] = []

    async def commit(_context: SessionProvisionCommitContext) -> None:
        events.append("commit")
        commit_started.set()
        await release_commit.wait()

    async def abort() -> None:
        events.append("abort")

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )
    commit_task = asyncio.create_task(_commit(provisioner, prepared))
    abort_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(commit_started.wait(), timeout=1)
        abort_task = asyncio.create_task(provisioner.abort_session_provision(prepared))
        release_commit.set()
        outcomes = cast(
            list[object],
            await asyncio.gather(
                commit_task,
                abort_task,
                return_exceptions=True,
            ),
        )
    finally:
        release_commit.set()
        tasks: list[asyncio.Task[Any]] = [commit_task]
        if abort_task is not None:
            tasks.append(abort_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert outcomes[0] is prepared.result
    assert isinstance(outcomes[1], SessionProvisionStateError)
    assert events == ["commit"]
    assert prepared.state is SessionProvisionState.COMMITTED


@pytest.mark.asyncio
async def test_abort_wins_race_and_conflicting_commit_has_no_side_effect() -> None:
    provisioner = _provisioner()
    abort_started = asyncio.Event()
    release_abort = asyncio.Event()
    events: list[str] = []

    async def commit(_context: SessionProvisionCommitContext) -> None:
        events.append("commit")

    async def abort() -> None:
        events.append("abort")
        abort_started.set()
        await release_abort.wait()

    prepared = _stage(
        provisioner,
        commit_hook=commit,
        abort_hook=abort,
    )
    abort_task = asyncio.create_task(provisioner.abort_session_provision(prepared))
    commit_task: asyncio.Task[SessionForkResult] | None = None
    try:
        await asyncio.wait_for(abort_started.wait(), timeout=1)
        commit_task = asyncio.create_task(_commit(provisioner, prepared))
        release_abort.set()
        outcomes = cast(
            list[object],
            await asyncio.gather(
                abort_task,
                commit_task,
                return_exceptions=True,
            ),
        )
    finally:
        release_abort.set()
        tasks: list[asyncio.Task[Any]] = [abort_task]
        if commit_task is not None:
            tasks.append(commit_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert outcomes[0] is None
    assert isinstance(outcomes[1], SessionProvisionStateError)
    assert events == ["abort"]
    assert prepared.state is SessionProvisionState.ABORTED
