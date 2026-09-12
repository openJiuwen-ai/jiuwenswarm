# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import contextvars

import pytest

from jiuwenswarm.runtime.session import (
    RuntimeSessionCoordinator,
    RuntimeSessionState,
    SessionPersistencePolicy,
    SessionWorkKind,
)
from jiuwenswarm.runtime.session.execution_registry import SessionExecutionRegistry
from jiuwenswarm.runtime.session.model import (
    SessionExecutionHandle,
    SessionExecutionState,
)

pytestmark = pytest.mark.unit


async def _register(
    coordinator: RuntimeSessionCoordinator, session_id: str = "session-a"
) -> None:
    await coordinator.register_session(
        session_id, "process", SessionPersistencePolicy.PERSISTENT
    )


@pytest.mark.asyncio
async def test_latest_first_within_session_and_parallel_across_sessions() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator, "a")
    await _register(coordinator, "b")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    other_started = asyncio.Event()
    order: list[str] = []

    async def first() -> str:
        order.append("first")
        first_started.set()
        await release_first.wait()
        return "first"

    async def queued(name: str) -> str:
        order.append(name)
        return name

    async def other() -> str:
        other_started.set()
        return "other"

    first_task = asyncio.create_task(
        coordinator.run_unary("a", "r1", SessionWorkKind.CHAT_UNARY, first)
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        coordinator.run_unary(
            "a", "r2", SessionWorkKind.CHAT_UNARY, lambda: queued("second")
        )
    )
    third_task = asyncio.create_task(
        coordinator.run_unary(
            "a", "r3", SessionWorkKind.CHAT_UNARY, lambda: queued("third")
        )
    )
    other_task = asyncio.create_task(
        coordinator.run_unary("b", "r4", SessionWorkKind.CHAT_UNARY, other)
    )
    await asyncio.wait_for(other_started.wait(), timeout=1)
    release_first.set()
    assert await asyncio.gather(first_task, second_task, third_task, other_task) == [
        "first",
        "second",
        "third",
        "other",
    ]
    assert order == ["first", "third", "second"]
    await coordinator.close()


@pytest.mark.asyncio
async def test_contextvars_are_captured_for_each_queued_operation() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    current_workspace = contextvars.ContextVar("current_workspace", default="none")
    blocker = asyncio.Event()
    started = asyncio.Event()

    async def first() -> str:
        started.set()
        await blocker.wait()
        return current_workspace.get()

    async def read_context() -> str:
        return current_workspace.get()

    current_workspace.set("first")
    first_task = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "r1", SessionWorkKind.CHAT_UNARY, first
        )
    )
    await started.wait()
    current_workspace.set("queued")
    queued_task = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "r2", SessionWorkKind.CHAT_UNARY, read_context
        )
    )
    await asyncio.sleep(0)
    current_workspace.set("caller-changed")
    blocker.set()
    assert await first_task == "first"
    assert await queued_task == "queued"
    await coordinator.close()


@pytest.mark.asyncio
async def test_cancel_by_request_execution_and_session() -> None:
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.2)
    await _register(coordinator)

    async def forever() -> None:
        await asyncio.Event().wait()

    first = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "request-1", SessionWorkKind.CHAT_UNARY, forever
        )
    )
    await asyncio.sleep(0.01)
    result = await coordinator.cancel_execution(
        "session-a", request_id="request-1"
    )
    assert result.matched == result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "request-2", SessionWorkKind.CHAT_UNARY, forever
        )
    )
    await asyncio.sleep(0.01)
    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    active = [item for item in snapshot.executions if not item.state.terminal]
    assert len(active) == 1
    result = await coordinator.cancel_execution(
        "session-a", execution_id=active[0].execution_id
    )
    assert result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await second

    third = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "request-3", SessionWorkKind.CHAT_UNARY, forever
        )
    )
    await asyncio.sleep(0.01)
    result = await coordinator.cancel_execution("session-a")
    assert result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await third
    await coordinator.close()


@pytest.mark.asyncio
async def test_cancelling_unary_caller_also_stops_owned_operation() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    operation_exited = asyncio.Event()
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_exited.set()

    caller = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "request", SessionWorkKind.CHAT_UNARY, work
        )
    )
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(operation_exited.wait(), timeout=1)
    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    assert snapshot.executions[-1].state is SessionExecutionState.CANCELLED
    await coordinator.close()


@pytest.mark.asyncio
async def test_cancel_queued_request_never_runs_its_operation() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    release = asyncio.Event()
    started = asyncio.Event()
    queued_ran = False

    async def first() -> None:
        started.set()
        await release.wait()

    async def queued() -> None:
        nonlocal queued_ran
        queued_ran = True

    first_caller = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "first", SessionWorkKind.CHAT_UNARY, first
        )
    )
    await started.wait()
    queued_caller = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "queued", SessionWorkKind.CHAT_UNARY, queued
        )
    )
    await asyncio.sleep(0)
    result = await coordinator.cancel_execution(
        "session-a", request_id="queued"
    )
    assert result.matched == result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await queued_caller
    assert queued_ran is False
    release.set()
    await first_caller
    await coordinator.close()


@pytest.mark.asyncio
async def test_stream_early_close_cancels_producer_and_waits_for_exit() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    producer_exited = asyncio.Event()

    async def source():
        try:
            yield "first"
            await asyncio.Event().wait()
        finally:
            producer_exited.set()

    stream = coordinator.run_stream(
        "session-a", "request", SessionWorkKind.CHAT_STREAM, source
    )
    assert await anext(stream) == "first"
    await stream.aclose()
    await asyncio.wait_for(producer_exited.wait(), timeout=1)
    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    assert snapshot.executions[-1].state is SessionExecutionState.CANCELLED
    await coordinator.close()


@pytest.mark.asyncio
async def test_cancel_timeout_keeps_resistant_execution_tracked() -> None:
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.01)
    await _register(coordinator)
    release = asyncio.Event()
    started = asyncio.Event()

    async def resistant() -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return "done"

    running = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "request", SessionWorkKind.CHAT_UNARY, resistant
        )
    )
    await started.wait()
    result = await coordinator.cancel_execution("session-a")
    assert len(result.timed_out) == 1
    snapshot = coordinator.get_execution(result.timed_out[0])
    assert snapshot is not None
    assert snapshot.state is SessionExecutionState.RUNNING
    assert snapshot.cancellation_requested is True
    release.set()
    assert await running == "done"
    await coordinator.close()


@pytest.mark.asyncio
async def test_old_generation_completion_does_not_clear_new_generation() -> None:
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.01)
    first_snapshot = await coordinator.register_session(
        "same", "process", SessionPersistencePolicy.PERSISTENT
    )
    release = asyncio.Event()
    started = asyncio.Event()

    async def resistant() -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return "old"

    old = asyncio.create_task(
        coordinator.run_unary(
            "same", "old", SessionWorkKind.CHAT_UNARY, resistant
        )
    )
    await started.wait()
    close_result = await coordinator.close_session(
        "same", generation=first_snapshot.generation
    )
    assert close_result.timed_out
    second_snapshot = await coordinator.register_session(
        "same", "process", SessionPersistencePolicy.PERSISTENT
    )
    assert second_snapshot.generation == first_snapshot.generation + 1
    assert await coordinator.run_unary(
        "same", "new", SessionWorkKind.CHAT_UNARY, lambda: asyncio.sleep(0, "new")
    ) == "new"
    release.set()
    assert await old == "old"
    current = coordinator.snapshot_session("same")
    assert current is not None
    assert current.generation == second_snapshot.generation
    assert current.state is RuntimeSessionState.READY
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_stops_new_work_and_cancels_active_execution() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "request", SessionWorkKind.CHAT_UNARY, work
        )
    )
    await started.wait()
    await coordinator.close()
    with pytest.raises(asyncio.CancelledError):
        await running
    with pytest.raises(RuntimeError, match="closed"):
        await coordinator.register_session(
            "new", "process", SessionPersistencePolicy.PERSISTENT
        )


def test_registry_retains_only_bounded_terminal_history() -> None:
    registry = SessionExecutionRegistry(terminal_capacity=1, terminal_ttl=60)
    first = SessionExecutionHandle(
        execution_id="first",
        session_id="session",
        request_id="r1",
        generation=1,
        work_kind=SessionWorkKind.CHAT_UNARY,
    )
    second = SessionExecutionHandle(
        execution_id="second",
        session_id="session",
        request_id="r2",
        generation=1,
        work_kind=SessionWorkKind.CHAT_UNARY,
    )
    registry.register(first)
    registry.mark_terminal(first, SessionExecutionState.SUCCEEDED)
    registry.register(second)
    registry.mark_terminal(second, SessionExecutionState.SUCCEEDED)
    assert registry.get("first") is None
    assert registry.get("second") is second


@pytest.mark.asyncio
async def test_external_stream_cancel_unblocks_consumer() -> None:
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.1)
    await _register(coordinator)
    started = asyncio.Event()

    async def source():
        started.set()
        yield "first"
        await asyncio.Event().wait()

    async def consume() -> list[str]:
        return [item async for item in coordinator.run_stream(
            "session-a", "request", SessionWorkKind.CHAT_STREAM, source
        )]

    consumer = asyncio.create_task(consume())
    await started.wait()
    result = await coordinator.cancel_execution("session-a", request_id="request")
    assert result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, timeout=1)
    await coordinator.close()


@pytest.mark.asyncio
async def test_control_input_bypasses_running_session_work_lane() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    answer_received = asyncio.Event()

    async def original():
        yield "chat.ask_user_question"
        await answer_received.wait()
        yield "chat.final"

    async def answer() -> str:
        answer_received.set()
        return "accepted"

    stream = coordinator.run_stream(
        "session-a",
        "original",
        SessionWorkKind.CHAT_STREAM,
        original,
        suspension_key=lambda item: (
            "interaction" if item == "chat.ask_user_question" else None
        ),
    )
    assert await anext(stream) == "chat.ask_user_question"
    assert await asyncio.wait_for(
        coordinator.deliver_control("session-a", "interaction", answer),
        timeout=0.2,
    ) == "accepted"
    assert await asyncio.wait_for(anext(stream), timeout=0.2) == "chat.final"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    parent = next(
        item
        for item in snapshot.executions
        if item.work_kind is SessionWorkKind.CHAT_STREAM
    )
    control = next(
        item
        for item in snapshot.executions
        if item.work_kind is SessionWorkKind.CONTROL_INPUT
    )
    assert control.parent_execution_id == parent.execution_id
    assert parent.state is SessionExecutionState.SUCCEEDED
    assert control.state is SessionExecutionState.SUCCEEDED
    await coordinator.close()


@pytest.mark.asyncio
async def test_control_input_resumes_work_after_output_stream_ends() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)

    async def original():
        yield "chat.ask_user_question"

    stream = coordinator.run_stream(
        "session-a",
        "original",
        SessionWorkKind.CHAT_STREAM,
        original,
        suspension_key=lambda item: (
            "answer" if item == "chat.ask_user_question" else None
        ),
    )
    assert [item async for item in stream] == ["chat.ask_user_question"]
    waiting = coordinator.snapshot_session("session-a")
    assert waiting is not None
    parent = waiting.executions[0]
    assert parent.state is SessionExecutionState.WAITING_FOR_CONTROL

    assert await coordinator.deliver_control(
        "session-a",
        "answer",
        lambda: asyncio.sleep(0, result="chat.ask_user_question"),
        suspension_key=lambda item: (
            "second-answer" if item == "chat.ask_user_question" else None
        ),
    ) == "chat.ask_user_question"
    suspended_again = coordinator.snapshot_session("session-a")
    assert suspended_again is not None
    control = next(
        item
        for item in suspended_again.executions
        if item.work_kind is SessionWorkKind.CONTROL_INPUT
    )
    assert parent.execution_id == control.parent_execution_id
    completed_parent = next(
        item
        for item in suspended_again.executions
        if item.execution_id == parent.execution_id
    )
    assert completed_parent.state is SessionExecutionState.SUCCEEDED
    assert control.state is SessionExecutionState.WAITING_FOR_CONTROL

    assert await coordinator.deliver_control(
        "session-a", "second-answer", lambda: asyncio.sleep(0, result="continued")
    ) == "continued"
    completed = coordinator.snapshot_session("session-a")
    assert completed is not None
    assert all(item.state.terminal for item in completed.executions)
    await coordinator.close()


@pytest.mark.asyncio
async def test_new_work_supersedes_waiting_control() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)

    async def original():
        yield "chat.ask_user_question"

    assert [
        item
        async for item in coordinator.run_stream(
            "session-a",
            "original",
            SessionWorkKind.CHAT_STREAM,
            original,
            suspension_key=lambda item: (
                "answer" if item == "chat.ask_user_question" else None
            ),
        )
    ] == ["chat.ask_user_question"]
    assert await coordinator.run_unary(
        "session-a",
        "replacement",
        SessionWorkKind.CHAT_UNARY,
        lambda: asyncio.sleep(0, result="done"),
    ) == "done"

    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    original_execution = next(
        item for item in snapshot.executions if item.request_id == "original"
    )
    replacement_execution = next(
        item for item in snapshot.executions if item.request_id == "replacement"
    )
    assert original_execution.state is SessionExecutionState.CANCELLED
    assert original_execution.cancellation_requested is True
    assert replacement_execution.state is SessionExecutionState.SUCCEEDED
    await coordinator.close()


@pytest.mark.asyncio
async def test_goal_stream_runs_while_chat_lane_is_busy() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    chat_started = asyncio.Event()
    finish_chat = asyncio.Event()

    async def chat() -> None:
        chat_started.set()
        await finish_chat.wait()

    chat_task = asyncio.create_task(
        coordinator.run_unary(
            "session-a", "chat", SessionWorkKind.CHAT_UNARY, chat
        )
    )
    await chat_started.wait()

    async def goal():
        yield "goal.started"

    assert [
        item
        async for item in coordinator.run_stream(
            "session-a", "goal", SessionWorkKind.GOAL_STREAM, goal
        )
    ] == ["goal.started"]
    assert not chat_task.done()

    finish_chat.set()
    await chat_task
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_session_cancels_goal_stream() -> None:
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.1)
    await _register(coordinator)
    started = asyncio.Event()

    async def goal():
        started.set()
        yield "goal.started"
        await asyncio.Event().wait()

    async def consume() -> list[str]:
        return [
            item
            async for item in coordinator.run_stream(
                "session-a", "goal", SessionWorkKind.GOAL_STREAM, goal
            )
        ]

    consumer = asyncio.create_task(
        consume()
    )
    await started.wait()

    result = await coordinator.close_session("session-a")

    assert result.existed is True
    with pytest.raises(asyncio.CancelledError):
        await consumer
    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    assert snapshot.executions[-1].state is SessionExecutionState.CANCELLED
    await coordinator.close()


@pytest.mark.asyncio
async def test_control_input_matches_goal_interaction_id() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)

    async def interaction():
        yield "ask"

    for request_id, work_kind, control_id in (
        ("chat", SessionWorkKind.CHAT_STREAM, "chat-question"),
        ("goal", SessionWorkKind.GOAL_STREAM, "goal-question"),
    ):
        assert [
            item
            async for item in coordinator.run_stream(
                "session-a",
                request_id,
                work_kind,
                interaction,
                suspension_key=lambda _item, key=control_id: key,
            )
        ] == ["ask"]

    assert await coordinator.deliver_control(
        "session-a",
        "goal-question",
        lambda: asyncio.sleep(0, result="accepted"),
    ) == "accepted"
    snapshot = coordinator.snapshot_session("session-a")
    assert snapshot is not None
    goal = next(item for item in snapshot.executions if item.request_id == "goal")
    chat = next(item for item in snapshot.executions if item.request_id == "chat")
    control = next(
        item
        for item in snapshot.executions
        if item.parent_execution_id is not None
    )
    assert control.parent_execution_id == goal.execution_id
    assert goal.state is SessionExecutionState.SUCCEEDED
    assert chat.state is SessionExecutionState.WAITING_FOR_CONTROL
    await coordinator.close()


@pytest.mark.asyncio
async def test_control_input_requires_running_session_work() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    with pytest.raises(RuntimeError, match="no active execution"):
        await coordinator.deliver_control(
            "session-a", "interaction", lambda: asyncio.sleep(0)
        )
    await coordinator.close()


@pytest.mark.asyncio
async def test_control_input_cancellation_does_not_cancel_parent_work() -> None:
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    finish_parent = asyncio.Event()
    control_started = asyncio.Event()

    async def original():
        yield "waiting"
        await finish_parent.wait()
        yield "done"

    async def control() -> None:
        control_started.set()
        await asyncio.Event().wait()

    stream = coordinator.run_stream(
        "session-a",
        "original",
        SessionWorkKind.CHAT_STREAM,
        original,
        suspension_key=lambda item: "answer" if item == "waiting" else None,
    )
    assert await anext(stream) == "waiting"
    control_task = asyncio.create_task(
        coordinator.deliver_control("session-a", "answer", control)
    )
    await control_started.wait()
    result = await coordinator.cancel_execution(
        "session-a", request_id="answer"
    )
    assert result.matched == result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await control_task

    finish_parent.set()
    assert await anext(stream) == "done"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await coordinator.close()
