# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Direct Runtime tests for transport-neutral ``session.fork`` provisioning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.runtime import (
    AgentRuntime,
    RuntimeStateError,
    SessionForkInput,
    SessionForkResult,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
)


@dataclass
class _ForkState:
    events: list[str] = field(default_factory=list)
    allocation_result: str = "allocated-target"
    allocation_error: BaseException | None = None
    agent: Any = None
    allocation_calls: list[tuple[str, str | None]] = field(default_factory=list)
    lookup_calls: list[str] = field(default_factory=list)


class _AgentManager:
    def __init__(self, state: _ForkState) -> None:
        self._state = state

    async def create_session(
        self,
        *,
        channel_id: str,
        session_id: str | None,
    ) -> str:
        self._state.events.append("runtime.allocate")
        self._state.allocation_calls.append((channel_id, session_id))
        if self._state.allocation_error is not None:
            raise self._state.allocation_error
        return self._state.allocation_result

    def get_agent_nowait(self, channel_id: str) -> Any:
        self._state.events.append("agent.lookup")
        self._state.lookup_calls.append(channel_id)
        return self._state.agent

    async def cancel_all_inflight_work(self, _reason: str) -> None:
        self._state.events.append("runtime.cancel")

    async def cleanup(self) -> None:
        self._state.events.append("runtime.cleanup")


class _PlanController:
    def reset_session(self, _session_id: str) -> None:
        return None


def _runtime(state: _ForkState) -> AgentRuntime:
    async def initialize() -> None:
        state.events.append("runtime.start")

    return AgentRuntime(
        agent_manager=cast(Any, _AgentManager(state)),
        initializer=initialize,
        plan_controller=cast(Any, _PlanController()),
    )


def _input(*, target_session_id: str | None = "fork-target") -> SessionForkInput:
    return SessionForkInput(
        channel_id="tui",
        source_session_id="fork-source",
        target_session_id=target_session_id,
        title="Forked session",
    )


def _result(target_session_id: str = "fork-target") -> dict[str, str]:
    return {
        "session_id": target_session_id,
        "source_session_id": "fork-source",
        "title": "Forked session",
    }


@pytest.mark.asyncio
async def test_explicit_target_preserves_business_order_agent_arguments_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState()
    card = object()
    deep_agent = SimpleNamespace(card=card)

    async def ensure_instance() -> Any:
        state.events.append("agent.ensure")
        return deep_agent

    state.agent = SimpleNamespace(ensure_instance=ensure_instance)

    def fork_session(**kwargs: Any) -> dict[str, str]:
        assert kwargs == {
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "title": "Forked session",
            "channel_id": "tui",
        }
        state.events.append("fork.filesystem")
        return _result()

    async def copy_session_context(
        selected_agent: Any,
        source_session_id: str,
        target_session_id: str,
    ) -> bool:
        assert selected_agent is deep_agent
        assert source_session_id == "fork-source"
        assert target_session_id == "fork-target"
        state.events.append("fork.context")
        return True

    async def copy_session_state(**kwargs: Any) -> bool:
        assert kwargs == {
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "card": card,
            "deep_agent": deep_agent,
        }
        state.events.append("fork.state")
        return True

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_context",
        copy_session_context,
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
    )
    runtime = _runtime(state)
    try:
        await runtime.start()
        prepared = await runtime.prepare_session_fork(_input())

        assert state.events == [
            "runtime.start",
            "fork.filesystem",
            "agent.lookup",
            "agent.ensure",
            "fork.context",
            "fork.state",
        ]
        assert state.allocation_calls == []
        assert prepared.result == SessionForkResult(
            channel_id="tui",
            source_session_id="fork-source",
            session_id="fork-target",
            title="Forked session",
        )
        assert prepared.state is SessionProvisionState.PREPARED
        assert prepared.commit_timing is (
            SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
        )

        committed = await runtime.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )

        assert committed is prepared.result
        assert prepared.state is SessionProvisionState.COMMITTED
        assert state.events[-1] == "fork.state"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_automatic_target_allocates_before_copy_and_supports_no_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState(agent=None)
    runtime = _runtime(state)
    copy_context = AsyncMock()

    def fork_session(**kwargs: Any) -> dict[str, str]:
        assert kwargs["target_session_id"] == "allocated-target"
        state.events.append("fork.filesystem")
        return {
            **_result("allocated-target"),
            "title": "Forked session (2)",
        }

    async def copy_session_state(**kwargs: Any) -> bool:
        assert kwargs["source_session_id"] == "fork-source"
        assert kwargs["target_session_id"] == "allocated-target"
        assert kwargs["deep_agent"] is None
        assert kwargs["card"].id == "jiuwenswarm"
        assert kwargs["card"].name == "jiuwenswarm"
        state.events.append("fork.state")
        return True

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(session_ops_service, "copy_session_context", copy_context)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
    )
    try:
        await runtime.start()
        prepared = await runtime.prepare_session_fork(_input(target_session_id=None))

        assert state.events == [
            "runtime.start",
            "runtime.allocate",
            "fork.filesystem",
            "agent.lookup",
            "fork.state",
        ]
        assert state.allocation_calls == [("tui", None)]
        copy_context.assert_not_awaited()
        assert prepared.result == SessionForkResult(
            channel_id="tui",
            source_session_id="fork-source",
            session_id="allocated-target",
            title="Forked session (2)",
        )
        await runtime.abort_session_provision(prepared)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_channel_id_is_forwarded_without_new_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState(agent=None)

    def fork_session(**kwargs: Any) -> dict[str, str]:
        assert kwargs["channel_id"] == " tui "
        return _result()

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        AsyncMock(return_value=True),
    )
    runtime = _runtime(state)
    try:
        await runtime.start()
        prepared = await runtime.prepare_session_fork(
            SessionForkInput(
                channel_id=" tui ",
                source_session_id="fork-source",
                target_session_id="fork-target",
                title="Forked session",
            )
        )

        assert prepared.result.channel_id == " tui "
        assert state.lookup_calls == [" tui "]
        await runtime.abort_session_provision(prepared)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("best_effort_stage", ["context", "state"])
async def test_false_context_or_state_copy_remains_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    best_effort_stage: str,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState()
    deep_agent = SimpleNamespace(card=object())
    state.agent = SimpleNamespace(ensure_instance=AsyncMock(return_value=deep_agent))

    def fork_session(**_kwargs: Any) -> dict[str, str]:
        state.events.append("fork.filesystem")
        return _result()

    async def copy_session_context(*_args: Any) -> bool:
        state.events.append("fork.context")
        return best_effort_stage != "context"

    async def copy_session_state(**_kwargs: Any) -> bool:
        state.events.append("fork.state")
        return best_effort_stage != "state"

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_context",
        copy_session_context,
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
    )
    runtime = _runtime(state)
    try:
        await runtime.start()
        prepared = await runtime.prepare_session_fork(_input())

        assert prepared.result.session_id == "fork-target"
        assert state.events == [
            "runtime.start",
            "fork.filesystem",
            "agent.lookup",
            "fork.context",
            "fork.state",
        ]
        await runtime.abort_session_provision(prepared)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_target_allocation_failure_short_circuits_all_copy_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    failure = RuntimeError("target allocation failed")
    state = _ForkState(allocation_error=failure)
    fork_session = MagicMock()
    copy_context = AsyncMock()
    copy_state = AsyncMock()
    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(session_ops_service, "copy_session_context", copy_context)
    monkeypatch.setattr(session_ops_service, "copy_session_state", copy_state)
    runtime = _runtime(state)
    try:
        await runtime.start()
        with pytest.raises(RuntimeError) as captured:
            await runtime.prepare_session_fork(_input(target_session_id=None))

        assert captured.value is failure
        assert state.events == ["runtime.start", "runtime.allocate"]
        fork_session.assert_not_called()
        copy_context.assert_not_awaited()
        copy_state.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_missing_source_is_bad_request_before_target_allocation() -> None:
    state = _ForkState()
    runtime = _runtime(state)
    try:
        await runtime.start()
        with pytest.raises(SessionProvisionError) as captured:
            await runtime.prepare_session_fork(
                SessionForkInput(
                    channel_id="tui",
                    source_session_id="  ",
                    target_session_id=None,
                )
            )

        assert str(captured.value) == "source_session_id is required"
        assert captured.value.code == "BAD_REQUEST"
        assert state.events == ["runtime.start"]
        assert state.allocation_calls == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ("invalid fork request", "BAD_REQUEST"),
        ("source session not found", "NOT_FOUND"),
        ("target session already exists", "ALREADY_EXISTS"),
    ],
)
async def test_value_error_preserves_legacy_code_mapping_and_stops_copy(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_code: str,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    failure = ValueError(message)
    state = _ForkState()
    copy_context = AsyncMock()
    copy_state = AsyncMock()
    monkeypatch.setattr(
        session_ops_service,
        "fork_session",
        MagicMock(side_effect=failure),
    )
    monkeypatch.setattr(session_ops_service, "copy_session_context", copy_context)
    monkeypatch.setattr(session_ops_service, "copy_session_state", copy_state)
    runtime = _runtime(state)
    try:
        await runtime.start()
        with pytest.raises(SessionProvisionError) as captured:
            await runtime.prepare_session_fork(_input())

        assert str(captured.value) == message
        assert captured.value.code == expected_code
        assert captured.value.__cause__ is failure
        copy_context.assert_not_awaited()
        copy_state.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_stage", ["context", "state"])
async def test_copy_cancellation_propagates_same_error_without_prepared_result(
    monkeypatch: pytest.MonkeyPatch,
    cancelled_stage: str,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState()
    deep_agent = SimpleNamespace(card=object())
    state.agent = SimpleNamespace(ensure_instance=AsyncMock(return_value=deep_agent))
    cancellation = asyncio.CancelledError(f"{cancelled_stage} cancelled")

    def fork_session(**_kwargs: Any) -> dict[str, str]:
        state.events.append("fork.filesystem")
        return _result()

    async def copy_session_context(*_args: Any) -> bool:
        state.events.append("fork.context")
        if cancelled_stage == "context":
            raise cancellation
        return True

    async def copy_session_state(**_kwargs: Any) -> bool:
        state.events.append("fork.state")
        if cancelled_stage == "state":
            raise cancellation
        return True

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_context",
        copy_session_context,
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        copy_session_state,
    )
    runtime = _runtime(state)
    try:
        await runtime.start()
        with pytest.raises(asyncio.CancelledError) as captured:
            await runtime.prepare_session_fork(_input())

        assert captured.value is cancellation
        expected_tail = ["fork.filesystem", "agent.lookup", "fork.context"]
        if cancelled_stage == "state":
            expected_tail.extend(["fork.state"])
        assert state.events[1:] == expected_tail
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", ["commit", "abort"])
async def test_close_fails_fast_until_prepared_fork_is_finalized(
    monkeypatch: pytest.MonkeyPatch,
    finalize: str,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState(agent=None)
    monkeypatch.setattr(
        session_ops_service, "fork_session", MagicMock(return_value=_result())
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        AsyncMock(return_value=True),
    )
    runtime = _runtime(state)
    prepared = None
    try:
        await runtime.start()
        prepared = await runtime.prepare_session_fork(_input())

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
            )
            assert prepared.state is SessionProvisionState.COMMITTED
        else:
            await runtime.abort_session_provision(prepared)
            assert prepared.state is SessionProvisionState.ABORTED

        await runtime.close()
        await runtime.close()
        assert state.events.count("runtime.cancel") == 1
        assert state.events.count("runtime.cleanup") == 1
    finally:
        if not runtime.closed:
            if (
                prepared is not None
                and prepared.state is SessionProvisionState.PREPARED
            ):
                await runtime.abort_session_provision(prepared)
            await runtime.close()


@pytest.mark.asyncio
async def test_close_fails_fast_while_session_fork_prepare_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service

    state = _ForkState()
    prepare_entered = asyncio.Event()
    release_prepare = asyncio.Event()
    deep_agent = SimpleNamespace(card=object())

    async def ensure_instance() -> Any:
        state.events.append("agent.ensure")
        prepare_entered.set()
        await release_prepare.wait()
        return deep_agent

    state.agent = SimpleNamespace(ensure_instance=ensure_instance)
    monkeypatch.setattr(
        session_ops_service, "fork_session", MagicMock(return_value=_result())
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_context",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        session_ops_service,
        "copy_session_state",
        AsyncMock(return_value=True),
    )
    runtime = _runtime(state)
    prepared = None
    prepare_task = None
    try:
        await runtime.start()
        prepare_task = asyncio.create_task(runtime.prepare_session_fork(_input()))
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


@pytest.mark.asyncio
async def test_closed_runtime_still_rejects_new_session_fork_prepare() -> None:
    runtime = _runtime(_ForkState())
    await runtime.start()
    await runtime.close()

    with pytest.raises(RuntimeStateError, match="runtime is already closed"):
        await runtime.prepare_session_fork(_input())
