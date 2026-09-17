# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming answers preserve Runtime authority, observation and cleanup."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.channels.process_cli.client import (
    InProcessRuntimeClient,
    ProcessCliBoundaryError,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.agent_definition import (
    RuntimeAgentDefinition,
    RuntimeAgentExecution,
)
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.interaction import InteractionAnswerInput
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session.model import SessionExecutionState, SessionWorkKind
from jiuwenswarm.runtime.session_catalog import SessionCatalogError


SESSION_ID = "sdk_session"


def _chunk(request: AgentRequest, event_type: str, **payload: object):
    return AgentResponseChunk(
        request_id=request.request_id,
        channel_id=request.channel_id,
        payload={"event_type": event_type, **payload},
    )


def _request() -> AgentRequest:
    return AgentRequest(
        request_id="root-request",
        channel_id="process_cli",
        session_id=SESSION_ID,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"query": "hello", "mode": "agent.code.normal", "work_mode": "code"},
    )


def _answer(
    interaction_id: str = "question_1",
    *,
    source: str = "ask_user_interrupt",
    mode: str = "agent.code.normal",
    channel_id: str = "process_cli",
    session_id: str = SESSION_ID,
) -> InteractionAnswerInput:
    return InteractionAnswerInput(
        request_id=f"answer-{interaction_id}",
        channel_id=channel_id,
        session_id=session_id,
        interaction_id=interaction_id,
        source=source,
        mode=mode,
        work_mode="code",
        answers=({"question": "Continue?", "selected_options": ["approve"]},),
    )


async def _collect(stream):
    async with aclosing(stream):
        return [event async for event in stream]


def _execution(snapshot, request_id: str):
    for execution in snapshot.executions:
        if execution.request_id == request_id:
            return execution
    raise AssertionError(f"missing execution for {request_id}")


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch):
    async def original(request):
        yield _chunk(
            request,
            "chat.ask_user_question",
            request_id="question_1",
            source="ask_user_interrupt",
        )

    async def delivered(request):
        yield _chunk(request, "runtime.accepted")

    agent = SimpleNamespace(
        process_message_stream=original,
        deliver_control_input=delivered,
    )
    manager = SimpleNamespace(
        create_session=AsyncMock(return_value=SESSION_ID),
        get_agent_for_session_nowait=Mock(return_value=agent),
        begin_foreground_chat=AsyncMock(),
        end_foreground_chat=AsyncMock(),
        cancel_all_inflight_work=AsyncMock(),
        cleanup_session_runtime=AsyncMock(return_value=True),
        cleanup=AsyncMock(),
    )
    plan = SimpleNamespace(
        ensure_state=AsyncMock(return_value=SimpleNamespace(events=[])),
        check_post_process_exit=AsyncMock(return_value=[]),
        reset_session=Mock(),
    )
    admission = SimpleNamespace(
        begin_user=AsyncMock(),
        end_user=AsyncMock(),
        mark_interaction_pending=AsyncMock(),
        clear_interaction_pending=AsyncMock(),
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=plan,
        admission_controller=admission,
    )
    monkeypatch.setattr(runtime, "get_session", Mock(return_value=None))
    monkeypatch.setattr(runtime, "describe_session", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_trigger_before_chat_request_hook", AsyncMock())
    monkeypatch.setattr(
        runtime, "_prepare_chat_turn", AsyncMock(return_value=("code", "normal", agent))
    )
    client = InProcessRuntimeClient(runtime)
    await client.start()
    await client.create_or_resume_session(channel_id="process_cli", session_id=None)
    value = SimpleNamespace(
        runtime=runtime,
        client=client,
        agent=agent,
        manager=manager,
        admission=admission,
        coordinator=runtime._session_coordinator,
    )
    try:
        yield value
    finally:
        await runtime.close()


async def _seed_waiting(harness) -> None:
    events = await _collect(harness.runtime.stream(_request(), trigger_hook=False))
    assert [event.event_type for event in events] == ["chat.ask_user_question"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "ask_user_interrupt",
        "permission_interrupt",
        "confirm_interrupt",
        "evolution_interrupt",
    ],
)
async def test_resumed_events_are_visible_before_eof(harness, source: str) -> None:
    await _seed_waiting(harness)
    finish = asyncio.Event()
    closed = []

    async def delivered(request):
        assert get_current_runtime() is harness.runtime
        assert request.params["source"] == source
        assert request.params["request_id"] == "question_1"
        assert request.params["supports_user_interaction"] is True
        assert request.req_method is ReqMethod.CHAT_SEND
        try:
            yield _chunk(request, "chat.delta", content="first")
            await finish.wait()
            yield _chunk(request, "chat.final", content="first and last")
        finally:
            closed.append(get_current_runtime())

    harness.agent.deliver_control_input = delivered
    stream = harness.client.stream_interaction_answer(_answer(source=source))
    async with aclosing(stream):
        async with asyncio.timeout(0.5):
            first = await anext(stream)
        assert first.event_type == "chat.delta"
        assert first.request_id == "answer-question_1"
        assert get_current_runtime() is None
        assert not finish.is_set()
        finish.set()
        remaining = [event async for event in stream]

    assert [event.event_type for event in remaining] == ["chat.final"]
    assert closed == [harness.runtime]
    snapshot = harness.coordinator.snapshot_session(SESSION_ID)
    assert all(
        item.state is SessionExecutionState.SUCCEEDED for item in snapshot.executions
    )


@pytest.mark.asyncio
async def test_second_question_streams_before_second_answer_and_root_eof(
    harness,
) -> None:
    await _seed_waiting(harness)
    received_second = asyncio.Event()

    async def delivered(request):
        if request.params["request_id"] == "question_1":
            yield _chunk(
                request,
                "chat.ask_user_question",
                request_id="question_2",
                source="ask_user_interrupt",
            )
            await received_second.wait()
            yield _chunk(request, "chat.final", content="done")
        else:
            received_second.set()
            yield _chunk(request, "runtime.accepted")

    harness.agent.deliver_control_input = delivered
    stream = harness.client.stream_interaction_answer(_answer())
    async with aclosing(stream):
        async with asyncio.timeout(0.5):
            second = await anext(stream)
        assert second.payload["request_id"] == "question_2"
        assert not received_second.is_set()
        acknowledgement = await _collect(
            harness.client.stream_interaction_answer(_answer("question_2"))
        )
        assert [event.event_type for event in acknowledgement] == ["runtime.accepted"]
        remaining = [event async for event in stream]
    assert [event.event_type for event in remaining] == ["chat.final"]
    harness.admission.mark_interaction_pending.assert_any_await(
        SESSION_ID, "question_2"
    )
    harness.admission.clear_interaction_pending.assert_any_await(
        SESSION_ID, "question_2"
    )
    snapshot = harness.coordinator.snapshot_session(SESSION_ID)
    assert all(item.state.terminal for item in snapshot.executions)


@pytest.mark.asyncio
async def test_live_original_keeps_output_and_new_question_survives_answer_ack(
    harness,
) -> None:
    first_received = asyncio.Event()
    second_published = asyncio.Event()
    second_received = asyncio.Event()

    async def original(request):
        yield _chunk(request, "chat.ask_user_question", request_id="question_1")
        await first_received.wait()
        second_published.set()
        yield _chunk(request, "chat.ask_user_question", request_id="question_2")
        await second_received.wait()
        yield _chunk(request, "chat.final", content="original output")

    async def delivered(request):
        if request.params["request_id"] == "question_1":
            first_received.set()
            await second_published.wait()
        else:
            second_received.set()
        yield _chunk(request, "runtime.accepted")

    harness.agent.process_message_stream = original
    harness.agent.deliver_control_input = delivered
    stream = harness.runtime.stream(_request(), trigger_hook=False)
    async with aclosing(stream):
        assert (await anext(stream)).payload["request_id"] == "question_1"
        async with asyncio.timeout(0.5):
            await _collect(harness.client.stream_interaction_answer(_answer()))
        second = await anext(stream)
        assert second.payload["request_id"] == "question_2"
        await _collect(harness.client.stream_interaction_answer(_answer("question_2")))
        final = [event async for event in stream]
    assert final[-1].payload["content"] == "original output"


@pytest.mark.asyncio
async def test_original_eof_before_old_ack_preserves_next_permission(harness) -> None:
    first_received = asyncio.Event()
    release_ack = asyncio.Event()

    async def original(request):
        yield _chunk(request, "chat.ask_user_question", request_id="question_1")
        await first_received.wait()
        yield _chunk(
            request,
            "chat.ask_user_question",
            request_id="permission_2",
            source="permission_interrupt",
        )

    async def delivered(request):
        if request.params["request_id"] == "question_1":
            first_received.set()
            await release_ack.wait()
            yield _chunk(request, "runtime.accepted")
        else:
            assert request.params["source"] == "permission_interrupt"
            yield _chunk(request, "chat.final", content="approved continuation")

    harness.agent.process_message_stream = original
    harness.agent.deliver_control_input = delivered
    stream = harness.runtime.stream(_request(), trigger_hook=False)
    async with aclosing(stream):
        assert (await anext(stream)).payload["request_id"] == "question_1"
        first_answer = asyncio.create_task(
            _collect(harness.client.stream_interaction_answer(_answer()))
        )
        try:
            remaining = [event async for event in stream]
            assert remaining[-1].payload["request_id"] == "permission_2"
            before_ack = harness.coordinator.snapshot_session(SESSION_ID)
            assert (
                _execution(before_ack, "root-request").state
                is SessionExecutionState.WAITING_FOR_CONTROL
            )
            release_ack.set()
            await first_answer
            after_ack = harness.coordinator.snapshot_session(SESSION_ID)
            parent = _execution(after_ack, "root-request")
            assert parent.state is SessionExecutionState.WAITING_FOR_CONTROL
            assert parent.waiting_control_id == "permission_2"
            events = await _collect(
                harness.client.stream_interaction_answer(
                    _answer("permission_2", source="permission_interrupt")
                )
            )
            assert events[-1].payload["content"] == "approved continuation"
        finally:
            first_answer.cancel()
            await asyncio.gather(first_answer, return_exceptions=True)
    snapshot = harness.coordinator.snapshot_session(SESSION_ID)
    assert all(item.state.terminal for item in snapshot.executions)


@pytest.mark.asyncio
async def test_wrong_or_completed_interaction_never_reaches_agent(harness) -> None:
    await _seed_waiting(harness)
    delivered = Mock(wraps=harness.agent.deliver_control_input)
    harness.agent.deliver_control_input = delivered
    with pytest.raises(RuntimeError, match="no active execution"):
        await _collect(harness.client.stream_interaction_answer(_answer("wrong")))
    delivered.assert_not_called()
    await _collect(harness.client.stream_interaction_answer(_answer()))
    with pytest.raises(RuntimeError, match="no active execution"):
        await _collect(harness.client.stream_interaction_answer(_answer()))
    assert delivered.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_answer_does_not_deliver_twice(harness) -> None:
    await _seed_waiting(harness)
    finish = asyncio.Event()

    async def delivered(request):
        yield _chunk(request, "runtime.accepted")
        await finish.wait()

    harness.agent.deliver_control_input = delivered
    stream = harness.client.stream_interaction_answer(_answer())
    async with aclosing(stream):
        await anext(stream)
        with pytest.raises(RuntimeError, match="already in progress"):
            await _collect(harness.client.stream_interaction_answer(_answer()))
        finish.set()
        assert [event async for event in stream] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("other_task", [False, True])
async def test_early_close_drains_control_generator_and_restores_context(
    harness, other_task: bool
) -> None:
    await _seed_waiting(harness)
    closed = []

    async def delivered(request):
        try:
            yield _chunk(request, "chat.delta", content="partial")
            await asyncio.Event().wait()
        finally:
            closed.append(get_current_runtime())

    harness.agent.deliver_control_input = delivered
    stream = harness.client.stream_interaction_answer(_answer())
    await anext(stream)
    assert get_current_runtime() is None
    if other_task:
        await asyncio.wait_for(stream.aclose(), timeout=0.5)
    else:
        await stream.aclose()
    assert closed == [harness.runtime]
    assert get_current_runtime() is None
    snapshot = harness.coordinator.snapshot_session(SESSION_ID)
    control = _execution(snapshot, "question_1")
    assert control.state is SessionExecutionState.CANCELLED
    assert (
        _execution(snapshot, "root-request").state
        is SessionExecutionState.WAITING_FOR_CONTROL
    )


@pytest.mark.asyncio
async def test_control_exception_propagates_and_can_retry_same_waiting_id(
    harness,
) -> None:
    await _seed_waiting(harness)
    closed = []
    original_delivery = harness.agent.deliver_control_input

    async def delivered(request):
        try:
            yield _chunk(request, "chat.delta", content="partial")
            raise ValueError("delivery failed")
        finally:
            closed.append(get_current_runtime())

    harness.agent.deliver_control_input = delivered
    with pytest.raises(ValueError, match="delivery failed"):
        await _collect(harness.client.stream_interaction_answer(_answer()))
    assert closed == [harness.runtime]
    harness.agent.deliver_control_input = original_delivery
    events = await _collect(harness.client.stream_interaction_answer(_answer()))
    assert [event.event_type for event in events] == ["runtime.accepted"]


@pytest.mark.asyncio
async def test_control_cleanup_exception_is_not_silenced(harness) -> None:
    await _seed_waiting(harness)

    async def delivered(request):
        try:
            yield _chunk(request, "chat.delta", content="partial")
        finally:
            raise RuntimeError("control cleanup failed")

    harness.agent.deliver_control_input = delivered
    stream = harness.client.stream_interaction_answer(_answer())
    await anext(stream)
    with pytest.raises(RuntimeError, match="control cleanup failed"):
        await stream.aclose()
    assert get_current_runtime() is None
    snapshot = harness.coordinator.snapshot_session(SESSION_ID)
    assert _execution(snapshot, "question_1").state is SessionExecutionState.FAILED


@pytest.mark.asyncio
async def test_coordinator_cancellation_drains_active_control_consumer(harness) -> None:
    await _seed_waiting(harness)
    entered = asyncio.Event()
    closed = []

    async def delivered(request):
        try:
            entered.set()
            yield _chunk(request, "runtime.accepted")
            await asyncio.Event().wait()
        finally:
            closed.append(get_current_runtime())

    harness.agent.deliver_control_input = delivered
    consumer = asyncio.create_task(
        _collect(harness.client.stream_interaction_answer(_answer()))
    )
    await entered.wait()
    result = await harness.coordinator.cancel_execution(
        SESSION_ID, request_id="question_1"
    )
    assert result.cancelled == 1
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert closed == [harness.runtime]


@pytest.mark.asyncio
async def test_ordinary_typed_answer_preserves_compatibility_api(
    harness, monkeypatch
) -> None:
    expected = RuntimeEvent.control(
        request_id="ordinary",
        channel_id="process_cli",
        session_id=SESSION_ID,
        payload={"event_type": "runtime.accepted"},
    )
    compatibility = AsyncMock(return_value=[expected])
    monkeypatch.setattr(harness.runtime, "answer_interaction", compatibility)
    answer = _answer(source="skill_evolution_approval")

    assert await _collect(harness.client.stream_interaction_answer(answer)) == [
        expected
    ]
    request = compatibility.await_args.args[0]
    assert request.req_method is ReqMethod.CHAT_ANSWER


@pytest.mark.asyncio
async def test_plan_answer_keeps_custom_owner_and_stream_options(
    harness, monkeypatch
) -> None:
    owner = RuntimeAgentExecution(
        definition=RuntimeAgentDefinition(name="custom_agent", instructions="Test."),
        mode="agent.code.plan",
    )
    monkeypatch.setattr(
        harness.runtime, "_agent_execution_owner", Mock(return_value=owner)
    )
    calls = []
    closed = []

    async def stream(request, **kwargs):
        calls.append((request, kwargs))
        try:
            yield RuntimeEvent.control(
                request_id=request.request_id,
                channel_id="process_cli",
                session_id=SESSION_ID,
                payload={"event_type": "chat.delta", "content": "resuming"},
            )
        finally:
            closed.append(get_current_runtime())

    monkeypatch.setattr(harness.runtime, "stream", stream)
    callback = AsyncMock()
    events = await _collect(
        harness.runtime.stream_interaction_answer(
            _answer(source="confirm_interrupt", mode="agent.code.plan"),
            trigger_hook=False,
            on_control_event=callback,
        )
    )
    assert [event.event_type for event in events] == ["chat.delta"]
    request, kwargs = calls[0]
    assert request.params["mode"] == "agent.code.plan"
    assert kwargs == {
        "trigger_hook": False,
        "on_control_event": callback,
        "_agent_execution": owner,
    }
    assert closed == [harness.runtime]
    assert get_current_runtime() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["close", "exhaust", "exception"])
async def test_real_facade_control_wrapper_closes_retained_adapter_stream(
    harness, monkeypatch, ending: str
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface

    await _seed_waiting(harness)
    closed = []
    answer = _answer()

    async def adapter_stream():
        try:
            yield _chunk(answer.to_agent_request(), "chat.delta", content="first")
            if ending == "exception":
                raise ValueError("adapter failed")
            yield _chunk(answer.to_agent_request(), "chat.final", content="done")
        finally:
            closed.append(get_current_runtime())

    # A strong reference prevents garbage collection from masking a missing
    # explicit close in the production Facade's control-stream wrapper.
    retained_inner = adapter_stream()
    facade = SimpleNamespace(
        _ensure_adapter=Mock(
            return_value=SimpleNamespace(
                process_message_stream_impl=Mock(return_value=retained_inner)
            )
        ),
        _adapter_mode_for_request=Mock(return_value="normal"),
        _session_manager=SimpleNamespace(get_session_id=Mock(return_value=SESSION_ID)),
        _build_inputs=Mock(return_value=({}, "", False)),
        reconcile_session_mcp=AsyncMock(),
    )
    monkeypatch.setattr(interface, "restore_chat_send_equipment_params", Mock())
    monkeypatch.setattr(
        interface, "compute_chat_send_mcp_needed", Mock(return_value=False)
    )
    harness.agent.deliver_control_input = lambda request: (
        interface.JiuWenSwarm.deliver_control_input(facade, request)
    )
    stream = harness.client.stream_interaction_answer(answer)
    try:
        async with aclosing(stream):
            assert (await anext(stream)).event_type == "chat.delta"
            if ending == "close":
                await stream.aclose()
            elif ending == "exception":
                with pytest.raises(ValueError, match="adapter failed"):
                    await anext(stream)
            else:
                remaining = [event async for event in stream]
                assert [event.event_type for event in remaining] == ["chat.final"]
        assert closed == [harness.runtime]
        assert get_current_runtime() is None
    finally:
        await retained_inner.aclose()


@pytest.mark.asyncio
async def test_foreign_session_and_channel_do_not_reach_control(harness) -> None:
    await harness.coordinator.register_session("foreign_session", "tui")
    delivered = Mock(wraps=harness.agent.deliver_control_input)
    harness.agent.deliver_control_input = delivered
    with pytest.raises(SessionCatalogError):
        await _collect(
            harness.client.stream_interaction_answer(
                _answer(session_id="foreign_session")
            )
        )
    with pytest.raises(ProcessCliBoundaryError):
        harness.client.stream_interaction_answer(_answer(channel_id="tui"))
    delivered.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_type_is_rejected_before_start(harness, monkeypatch) -> None:
    started = AsyncMock()
    monkeypatch.setattr(harness.runtime, "start", started)
    with pytest.raises(TypeError, match="InteractionAnswerInput"):
        await _collect(harness.runtime.stream_interaction_answer(object()))
    started.assert_not_awaited()


@pytest.mark.asyncio
async def test_awaitable_stream_factory_and_waiting_terminal_semantics(harness) -> None:
    await _seed_waiting(harness)

    async def chunks():
        yield "question_2"

    async def operation():
        return chunks()

    events = await _collect(
        harness.coordinator.deliver_control_stream(
            SESSION_ID,
            "question_1",
            operation,
            suspension_key=lambda value: value,
        )
    )
    assert events == ["question_2"]
    snapshot = harness.coordinator.snapshot_session(SESSION_ID)
    parent = _execution(snapshot, "root-request")
    control = _execution(snapshot, "question_1")
    assert parent.state is SessionExecutionState.SUCCEEDED
    assert control.work_kind is SessionWorkKind.CONTROL_INPUT
    assert control.parent_execution_id == parent.execution_id
    assert control.state is SessionExecutionState.WAITING_FOR_CONTROL
    assert control.waiting_control_id == "question_2"
