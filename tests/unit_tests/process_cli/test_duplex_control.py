# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Duplex command lifecycle at the public client boundary, without a model."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from jiuwenswarm.channels.process_cli import machine
from jiuwenswarm.channels.process_cli.duplex_control import DuplexController
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.protocol import (
    AgentSpec,
    OneShotRunInput,
    OneShotRunResult,
    WorkspaceSpec,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.interaction import InteractionAnswerInput
from tests.unit_tests.process_cli.test_machine import FakeClient


def _event(event_type: str, **payload: Any) -> RuntimeEvent:
    return RuntimeEvent(
        request_id="internal-operation",
        channel_id="process_cli",
        session_id="runtime-session",
        payload={"event_type": event_type, **payload},
        is_complete=event_type == "chat.final",
    )


def _card(card_id: str = "runtime-card-1", **payload: Any) -> RuntimeEvent:
    return _event(
        "chat.ask_user_question",
        request_id=card_id,
        source="ask_user_interrupt",
        **payload,
    )


class QueueReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        self.closed = False
        self.eof_seen = False
        self.reading = asyncio.Event()

    async def read_line(self) -> bytes | None:
        self.reading.set()
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        if item is None and not self.closed:
            self.eof_seen = True
        return item

    def send(self, value: dict[str, Any] | Exception | None) -> None:
        if isinstance(value, dict):
            self.queue.put_nowait(json.dumps(value).encode("utf-8"))
        else:
            self.queue.put_nowait(value)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)


class QueueStream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[RuntimeEvent | Exception | None] = asyncio.Queue()
        self.closed = False
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.consumer: asyncio.Task[Any] | None = None
        self.close_error: Exception | None = None

    def emit(self, item: RuntimeEvent | Exception | None) -> None:
        self.queue.put_nowait(item)

    def __aiter__(self) -> QueueStream:
        return self

    async def __anext__(self) -> RuntimeEvent:
        if self.closed:
            raise StopAsyncIteration
        self.consumer = asyncio.current_task()
        self.started.set()
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.finished.set()
        if self.close_error is not None:
            raise self.close_error


class DuplexClient(FakeClient):
    def __init__(self, *, descriptor: Any = None) -> None:
        super().__init__(events=[], descriptor=descriptor)
        self.original = QueueStream()
        self.answer_streams: list[QueueStream] = []
        self.answer_inputs: list[InteractionAnswerInput] = []
        self.answer_calls: asyncio.Queue[tuple[InteractionAnswerInput, QueueStream]] = (
            asyncio.Queue()
        )

    def stream(self, request: Any) -> QueueStream:
        super().stream(request)
        return self.original

    def stream_agent(self, request: Any, definition: Any) -> QueueStream:
        super().stream_agent(request, definition)
        return self.original

    def stream_interaction_answer(self, answer: InteractionAnswerInput) -> QueueStream:
        self.calls.append("typed_answer")
        stream = QueueStream()
        self.answer_inputs.append(answer)
        self.answer_streams.append(stream)
        self.answer_calls.put_nowait((answer, stream))
        return stream

    async def answer_interaction(self, _request: Any) -> None:
        raise AssertionError("duplex controls must use the typed streaming answer API")


class ObservedOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def write(self, value: str) -> int:
        written = super().write(value)
        record = json.loads(value)
        self.records.append(record)
        self.events.put_nowait(record)
        return written

    async def next_event(self, event_type: str) -> dict[str, Any]:
        async with asyncio.timeout(1):
            while True:
                record = await self.events.get()
                if record.get("event_type") == event_type:
                    return record


class RunHarness:
    def __init__(
        self,
        *,
        client: DuplexClient | None = None,
        run_input: OneShotRunInput | None = None,
    ) -> None:
        self.client = client or DuplexClient()
        self.reader = QueueReader()
        self.output = ObservedOutput()
        self.writer = OneShotWriter(self.output, request_id="external-run")
        self.control = DuplexController(self.reader, self.writer)
        self.run_input = run_input or OneShotRunInput(input="question")
        self.task: asyncio.Task[OneShotRunResult] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(
            machine.run_machine(
                self.run_input,
                self.writer,
                client_factory=lambda: self.client,
                control=self.control,
            ),
            name="test-duplex-run",
        )

    async def card(self) -> dict[str, Any]:
        return await self.output.next_event("interaction.requested")

    def answer_record(self, notice: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        record = {
            "schema_version": "0.1",
            "type": "answer",
            "request_id": notice["request_id"],
            "session_id": notice["session_id"],
            "interaction_id": notice["payload"]["interaction_id"],
            "answers": [{"id": "question-1", "value": "caller-answer"}],
        }
        record.update(overrides)
        return record

    async def answer(
        self, notice: dict[str, Any]
    ) -> tuple[InteractionAnswerInput, QueueStream]:
        self.reader.send(self.answer_record(notice))
        return await asyncio.wait_for(self.client.answer_calls.get(), timeout=1)

    def cancel(self, *, session_id: str | None = None) -> None:
        self.reader.send(
            {
                "schema_version": "0.1",
                "type": "cancel",
                "request_id": "external-run",
                "session_id": session_id,
            }
        )

    async def finish(self) -> OneShotRunResult:
        assert self.task is not None
        result = await asyncio.wait_for(self.task, timeout=1)
        self.assert_clean()
        return result

    def assert_clean(self) -> None:
        assert self.reader.closed
        assert self.client.calls[-1] == "close"
        if self.client.request is not None:
            assert self.client.original.closed
        assert all(stream.closed for stream in self.client.answer_streams)
        streams = [self.client.original, *self.client.answer_streams]
        for stream in streams:
            if stream.started.is_set():
                assert stream.closed
                assert stream.consumer is not None
                assert stream.consumer.done()
        live = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name() in {"one-shot-controls", "one-shot-runtime-stream"}
        ]
        assert live == []
        assert not self.writer.final_written

    async def cleanup_test(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.wait_for(self.task, timeout=1)


@pytest_asyncio.fixture
async def run_factory() -> AsyncIterator[Callable[..., RunHarness]]:
    runs: list[RunHarness] = []

    def create(**kwargs: Any) -> RunHarness:
        harness = RunHarness(**kwargs)
        runs.append(harness)
        return harness

    yield create
    for harness in runs:
        await harness.cleanup_test()


@pytest.mark.asyncio
async def test_normal_completion_closes_reader_with_parent_input_still_open(
    run_factory,
) -> None:
    run = run_factory()
    run.client.original.emit(_event("chat.final", content="done"))
    run.client.original.emit(None)
    run.start()

    result = await run.finish()

    assert result.status == "completed"
    assert result.output == "done"
    assert result.error is None
    assert not run.reader.eof_seen
    assert run.client.answer_inputs == []
    assert run.client.request.params["supports_user_interaction"] is True
    assert [record["event_type"] for record in run.output.records] == [
        "run.started",
        "chat.final",
    ]
    assert run.client.cleaned_sessions == [("process_cli", "runtime-session")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_eof", [False, True], ids=["original-waiting", "original-eof"]
)
async def test_answer_continuation_works_with_either_original_stream_lifetime(
    original_eof: bool, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    if original_eof:
        run.client.original.emit(None)
    run.start()
    notice = await run.card()
    assert run.client.answer_inputs == []
    assert not run.task.done()

    answer, continuation = await run.answer(notice)
    if not original_eof:
        run.client.original.emit(None)
    continuation.emit(_event("chat.delta", delta="after "))
    continuation.emit(_event("chat.final", content="after answer"))
    continuation.emit(None)
    result = await run.finish()

    assert result.status == "completed"
    assert result.output == "after answer"
    assert answer.interaction_id == "runtime-card-1"
    assert answer.request_id != "external-run"
    assert notice["payload"]["interaction_id"] != answer.interaction_id
    assert {item["request_id"] for item in run.output.records} == {"external-run"}
    assert {item["session_id"] for item in run.output.records} == {"runtime-session"}


@pytest.mark.asyncio
async def test_two_successive_interactions_are_published_and_answered_in_real_time(
    run_factory,
) -> None:
    run = run_factory()
    run.client.original.emit(_card("card-one"))
    run.client.original.emit(None)
    run.start()
    first = await run.card()

    first_answer, first_stream = await run.answer(first)
    first_stream.emit(_card("card-two"))
    first_stream.emit(None)
    second = await run.card()
    assert not run.task.done()
    assert len(run.client.answer_inputs) == 1
    second_answer, second_stream = await run.answer(second)
    second_stream.emit(_event("chat.final", content="both answered"))
    second_stream.emit(None)

    result = await run.finish()

    assert result.status == "completed"
    assert result.output == "both answered"
    assert first["payload"]["interaction_id"] != second["payload"]["interaction_id"]
    assert first_answer.interaction_id == "card-one"
    assert second_answer.interaction_id == "card-two"
    assert first_answer.request_id != second_answer.request_id
    assert [record["sequence"] for record in run.output.records] == list(
        range(result.sequence)
    )


def _ack(kind: str) -> RuntimeEvent:
    if kind == "null":
        return RuntimeEvent(
            request_id="answer-internal",
            channel_id="process_cli",
            session_id="runtime-session",
            payload=None,
            is_complete=True,
        )
    event = _event(kind)
    event.is_complete = True
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runtime.accepted", "chat.answer_accepted", "null"])
async def test_answer_ack_or_null_completion_waits_for_original_continuation(
    kind: str, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(
        _event("chat.ask_user_question", request_id="regular-question", source="")
    )
    run.start()
    notice = await run.card()
    answer, answer_stream = await run.answer(notice)
    assert not answer.resumes_interrupted_turn
    assert answer.to_agent_request().req_method == ReqMethod.CHAT_ANSWER
    answer_stream.emit(_ack(kind))
    answer_stream.emit(None)
    await run.output.next_event("runtime.event" if kind == "null" else kind)
    await asyncio.sleep(0)
    assert not run.task.done()

    run.client.original.emit(_event("chat.final", content="original continuation"))
    run.client.original.emit(None)
    result = await run.finish()

    assert result.status == "completed"
    assert result.output == "original continuation"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runtime.accepted", "chat.answer_accepted", "null"])
async def test_answer_ack_or_null_completion_cannot_forge_success(
    kind: str, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    run.client.original.emit(None)
    run.start()
    _, answer_stream = await run.answer(await run.card())
    answer_stream.emit(_ack(kind))
    answer_stream.emit(None)

    result = await run.finish()

    assert result.status == "failed"
    assert result.error.code == "INCOMPLETE_RUN"
    assert result.output is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["ask_user_interrupt", "permission_interrupt", "confirm_interrupt"]
)
@pytest.mark.parametrize("locator", ["request_id", "interaction_id"])
async def test_typed_answer_copies_pending_source_metadata_card_and_owned_workspace(
    source: str, locator: str, run_factory
) -> None:
    workspace = WorkspaceSpec(
        cwd="owned-cwd", project_dir="owned-project", trusted_dirs=("owned-trusted",)
    )
    run = run_factory(
        run_input=OneShotRunInput(
            input="question", mode="agent.work.plan", workspace=workspace
        )
    )
    payload = {
        locator: "real-runtime-card",
        "source": source,
        "approval_schema": "approval-v1",
        "evolution_meta": {"change": {"id": "approved-change"}},
        "mode": "not-an-owned-mode",
        "cwd": "not-an-owned-cwd",
    }
    card = _event("plan.approval_required", **payload)
    original_payload = deepcopy(card.payload)
    run.client.original.emit(card)
    run.client.original.emit(None)
    run.start()
    notice = await run.card()
    assert run.client.answer_inputs == []
    assert card.payload == original_payload
    card.payload["source"] = "changed-after-observation"
    card.payload["evolution_meta"]["change"]["id"] = "changed-after-observation"

    answer, continuation = await run.answer(notice)
    continuation.emit(_event("chat.final", content="done"))
    continuation.emit(None)
    result = await run.finish()

    assert result.status == "completed"
    assert isinstance(answer, InteractionAnswerInput)
    assert answer.source == source
    assert answer.approval_schema == "approval-v1"
    assert answer.evolution_meta == {"change": {"id": "approved-change"}}
    assert answer.interaction_id == "real-runtime-card"
    assert answer.channel_id == "process_cli"
    assert answer.session_id == "runtime-session"
    assert answer.mode == "agent.work.plan"
    assert answer.work_mode == "work"
    assert answer.project_dir == "owned-project"
    assert answer.cwd == "owned-cwd"
    assert answer.trusted_dirs == ("owned-trusted",)
    assert answer.answers == ({"id": "question-1", "value": "caller-answer"},)
    assert answer.resumes_interrupted_turn
    runtime_request = answer.to_agent_request()
    assert runtime_request.req_method == ReqMethod.CHAT_SEND
    assert runtime_request.params["request_id"] == "real-runtime-card"


@pytest.mark.asyncio
async def test_custom_agent_uses_stream_agent_then_the_typed_streaming_answer_api(
    run_factory,
) -> None:
    agent = AgentSpec(name="custom-agent", instructions="custom instructions")
    run = run_factory(run_input=OneShotRunInput(input="question", agent=agent))
    run.client.original.emit(_card())
    run.client.original.emit(None)
    run.start()
    _, continuation = await run.answer(await run.card())
    continuation.emit(_event("chat.final", content="custom done"))
    continuation.emit(None)

    result = await run.finish()

    assert result.status == "completed"
    assert run.client.agent_definition == agent.to_dict()
    assert "stream_agent" in run.client.calls
    assert "stream" not in run.client.calls
    assert run.client.calls.count("typed_answer") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid,code",
    [
        ("wrong-run", "INVALID_CONTROL"),
        ("wrong-session", "INVALID_CONTROL"),
        ("wrong-token", "INTERACTION_NOT_PENDING"),
        ("second-run", "INVALID_CONTROL"),
        ("host-override", "INVALID_CONTROL"),
        ("duplicate", "INTERACTION_NOT_PENDING"),
    ],
)
async def test_invalid_controls_never_answer_a_different_or_consumed_interaction(
    invalid: str, code: str, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    run.start()
    notice = await run.card()
    record = run.answer_record(notice)
    if invalid == "wrong-run":
        record["request_id"] = "secret-other-run"
    elif invalid == "wrong-session":
        record["session_id"] = "secret-other-session"
    elif invalid == "wrong-token":
        record["interaction_id"] = "secret-other-token"
    elif invalid == "second-run":
        record = {"schema_version": "0.1", "type": "run", "input": "secret-prompt"}
    elif invalid == "host-override":
        record["source"] = "secret-override"
    else:
        await run.answer(notice)
    run.reader.send(record)

    result = await run.finish()

    assert result.status == "failed"
    assert result.exit_code == 2
    assert result.error.code == code
    assert len(run.client.answer_inputs) == int(invalid == "duplicate")
    assert "secret" not in result.error.message
    assert run.client.cancel_request.session_id == "runtime-session"
    assert run.client.cancel_request.params["target_request_id"] == ""


@pytest.mark.asyncio
async def test_answer_before_any_observed_interaction_is_rejected(run_factory) -> None:
    run = run_factory()
    run.start()
    started = await run.output.next_event("run.started")
    run.reader.send(
        {
            "schema_version": "0.1",
            "type": "answer",
            "request_id": started["request_id"],
            "session_id": started["session_id"],
            "interaction_id": "not-yet-issued",
            "answers": [{"value": "early"}],
        }
    )

    result = await run.finish()

    assert result.error.code == "INTERACTION_NOT_PENDING"
    assert run.client.answer_inputs == []


@pytest.mark.asyncio
async def test_waiting_permission_is_never_auto_approved_and_eof_cancels(
    run_factory,
) -> None:
    run = run_factory()
    run.client.original.emit(
        _event(
            "plan.approval_required",
            request_id="permission-card",
            source="permission_interrupt",
        )
    )
    run.start()
    await run.card()
    await asyncio.sleep(0.01)
    assert not run.task.done()
    assert run.client.answer_inputs == []

    run.reader.send(None)
    result = await run.finish()

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert result.error.code == "INPUT_CLOSED"
    assert run.client.answer_inputs == []


@pytest.mark.asyncio
async def test_cancel_during_startup_does_not_claim_or_cancel_a_session(
    run_factory,
) -> None:
    client = DuplexClient()
    client.hang_at = "start"
    run = run_factory(client=client)
    run.start()
    await asyncio.wait_for(client.blocked.wait(), timeout=1)

    run.cancel()
    result = await run.finish()

    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert result.error.code == "CANCELLED"
    assert result.session_id is None
    assert client.cancel_request is None
    assert client.cleaned_sessions == []
    assert client.calls == ["start", "close"]


@pytest.mark.asyncio
async def test_startup_deadline_closes_control_reader_without_claiming_a_session(
    run_factory,
) -> None:
    client = DuplexClient()
    client.hang_at = "start"
    run = run_factory(
        client=client,
        run_input=OneShotRunInput(input="question", timeout_seconds=0.02),
    )
    run.start()

    result = await run.finish()

    assert result.status == "timed_out"
    assert result.error.code == "TIMEOUT"
    assert result.session_id is None
    assert client.cancel_request is None
    assert client.cleaned_sessions == []
    assert client.calls == ["start", "close"]


@pytest.mark.asyncio
async def test_cancel_after_resume_targets_the_exact_session_not_original_operation(
    run_factory,
) -> None:
    client = DuplexClient(descriptor=SimpleNamespace(mode="agent.code.normal"))
    run = run_factory(
        client=client,
        run_input=OneShotRunInput(input="continue", session_id="existing-session"),
    )
    client.original.emit(_card())
    client.original.emit(None)
    run.start()
    answer, continuation = await run.answer(await run.card())
    await asyncio.wait_for(continuation.started.wait(), timeout=1)

    run.cancel(session_id="existing-session")
    result = await run.finish()

    assert result.status == "cancelled"
    assert result.session_id == "existing-session"
    cancel = client.cancel_request
    assert cancel.req_method == ReqMethod.CHAT_CANCEL
    assert cancel.session_id == "existing-session"
    assert cancel.request_id == "external-run"
    assert cancel.request_id != answer.request_id
    assert cancel.params["target_request_id"] == ""
    assert client.cleaned_sessions == [("process_cli", "existing-session")]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_answer", [False, True])
async def test_deadline_covers_waiting_for_host_and_answer_continuation(
    after_answer: bool, run_factory
) -> None:
    run = run_factory(run_input=OneShotRunInput(input="question", timeout_seconds=0.05))
    run.client.original.emit(_card())
    run.start()
    notice = await run.card()
    if after_answer:
        await run.answer(notice)

    result = await run.finish()

    assert result.status == "timed_out"
    assert result.exit_code == 124
    assert result.error.code == "TIMEOUT"
    assert run.client.cancel_request.params["target_request_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stream", ["original", "answer"])
async def test_either_stream_failure_cancels_and_closes_every_pump(
    failed_stream: str, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    run.start()
    _, continuation = await run.answer(await run.card())
    target = run.client.original if failed_stream == "original" else continuation
    target.emit(RuntimeError("secret-stream-failure"))

    result = await run.finish()

    assert result.status == "failed"
    assert result.error.code == "RUNTIME_FAILED"
    assert "secret" not in result.error.message
    assert run.client.original.closed
    assert continuation.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stream", ["original", "answer"])
async def test_stream_close_failure_is_not_silently_dropped(
    failed_stream: str, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    run.start()
    _, continuation = await run.answer(await run.card())
    target = run.client.original if failed_stream == "original" else continuation
    target.close_error = RuntimeError("secret-close-failure")
    target.emit(None)

    result = await run.finish()

    assert result.status == "failed"
    assert result.error.code == "RUNTIME_FAILED"
    assert "secret" not in result.error.message
    assert run.client.original.closed
    assert continuation.closed


@pytest.mark.asyncio
async def test_cancel_preserves_primary_reason_when_one_pump_close_fails(
    run_factory,
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    run.start()
    _, continuation = await run.answer(await run.card())
    await asyncio.wait_for(continuation.started.wait(), timeout=1)
    continuation.close_error = RuntimeError("secret-close-failure")

    run.cancel(session_id="runtime-session")
    result = await run.finish()

    assert result.status == "cancelled"
    assert result.error.code == "CANCELLED"
    assert "control_streams" in result.error.details["cleanup_errors"]
    assert run.client.original.closed
    assert continuation.closed
    assert run.client.calls[-2:] == ["cleanup_session", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["harness.activate_interaction", "missing-locator"])
async def test_unsupported_interaction_never_uses_legacy_harness_or_auto_answer(
    kind: str, run_factory
) -> None:
    run = run_factory()
    interaction = (
        _event("chat.ask_user_question")
        if kind == "missing-locator"
        else _event(kind, request_id="legacy-card")
    )
    run.client.original.emit(interaction)
    for _ in range(100):
        run.client.original.emit(_event("chat.delta", delta="must not continue"))
    run.start()

    result = await run.finish()

    assert result.status == "failed"
    assert result.error.code == "INTERACTION_UNSUPPORTED"
    assert result.output is None
    assert run.client.answer_inputs == []
    assert [record["event_type"] for record in run.output.records] == ["run.started"]


@pytest.mark.asyncio
async def test_cancelled_control_reader_propagates_without_protocol_failure() -> None:
    reader = QueueReader()
    writer = OneShotWriter(io.StringIO(), request_id="reader-cancel")
    control = DuplexController(reader, writer)
    task = asyncio.create_task(control._read_controls())
    try:
        async with asyncio.timeout(1):
            await reader.reading.wait()
            task.cancel("reader stopped")
            with pytest.raises(asyncio.CancelledError, match="reader stopped"):
                await task
        assert task.cancelled()
        assert control.failure is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.close()


@pytest.mark.asyncio
async def test_cancelled_pump_propagates_and_closes_stream() -> None:
    reader = QueueReader()
    writer = OneShotWriter(io.StringIO(), request_id="pump-cancel")
    control = DuplexController(reader, writer)
    stream = QueueStream()
    task = asyncio.create_task(control._pump("operation", stream))
    try:
        async with asyncio.timeout(1):
            await stream.started.wait()
            task.cancel("pump stopped")
            with pytest.raises(asyncio.CancelledError, match="pump stopped"):
                await task
        assert task.cancelled()
        assert stream.closed
        assert control.failure is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reader.close()


@pytest.mark.asyncio
async def test_reader_failure_is_safe_and_does_not_leave_pump_tasks(
    run_factory,
) -> None:
    run = run_factory()
    run.start()
    await run.output.next_event("run.started")
    run.reader.send(RuntimeError("secret-reader-error"))

    result = await run.finish()

    assert result.error.code == "INVALID_CONTROL"
    assert result.exit_code == 2
    assert "secret" not in result.error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", ["runtime.error", "chat.error", "failed.observation"]
)
async def test_runtime_error_after_pending_card_fails_without_host_answer_or_eof(
    event_type: str, run_factory
) -> None:
    run = run_factory()
    run.client.original.emit(_card())
    run.start()
    await run.card()
    error = _event(event_type, code="PENDING_FAILED", message="Execution failed.")
    if event_type == "failed.observation":
        error.ok = False
    run.client.original.emit(error)

    result = await run.finish()

    assert result.status == "failed"
    assert result.error.code == "PENDING_FAILED"
    assert not run.reader.eof_seen
    assert run.client.answer_inputs == []
    assert run.client.original.closed


@pytest.mark.asyncio
async def test_full_answer_lifecycle_leaves_no_new_asyncio_tasks(run_factory) -> None:
    before = asyncio.all_tasks()
    run = run_factory()
    run.client.original.emit(_card())
    run.client.original.emit(None)
    run.start()
    _, continuation = await run.answer(await run.card())
    continuation.emit(_event("chat.final", content="done"))
    continuation.emit(None)

    assert (await run.finish()).status == "completed"
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mode", ["cancel-producer", "drain-runtime-tail"])
async def test_full_output_queue_does_not_block_runtime_cancel_draining_producer(
    cancel_mode: str, monkeypatch: pytest.MonkeyPatch, run_factory
) -> None:
    saturated = asyncio.Event()
    drained = asyncio.Event()
    output_queues: list[asyncio.Queue] = []
    saturated_sizes: list[int] = []

    class ObservedQueue(asyncio.Queue):
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize=maxsize)
            if maxsize == 32:
                output_queues.append(self)

        async def put(self, item: Any) -> None:
            if self.maxsize == 32 and self.full():
                saturated_sizes.append(self.qsize())
                saturated.set()
            await super().put(item)

    class DrainingCancelClient(DuplexClient):
        async def cancel(self, request: Any) -> None:
            await super().cancel(request)
            assert saturated.is_set()
            assert len(output_queues) == 1
            assert output_queues[0].qsize() < output_queues[0].maxsize
            producer = self.original.consumer
            assert producer is not None
            if cancel_mode == "cancel-producer":
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            else:
                # Runtime cancellation can itself await the stream consumer
                # draining a final burst; it need not cancel the adapter task.
                for _ in range(1000):
                    self.original.emit(_event("chat.delta", delta="shutdown tail"))
                self.original.emit(_event("chat.final", content="shutdown tail"))
                self.original.emit(None)
                await producer
                assert not producer.cancelled()
                assert output_queues[0].qsize() < output_queues[0].maxsize
            drained.set()

    monkeypatch.setattr(asyncio, "Queue", ObservedQueue)
    monkeypatch.setattr(machine, "SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    client = DrainingCancelClient()
    run = run_factory(client=client)
    client.original.emit(
        _event("runtime.error", code="PRIMARY_FAILURE", message="Execution failed.")
    )
    for _ in range(100):
        client.original.emit(_event("chat.delta", delta="queued output"))
    run.start()

    result = await run.finish()

    assert saturated.is_set()
    assert saturated_sizes
    assert set(saturated_sizes) == {32}
    assert drained.is_set()
    assert result.status == "failed"
    assert result.error.code == "PRIMARY_FAILURE"
    assert "cleanup_errors" not in result.error.details
    assert result.output is None
    assert [record["event_type"] for record in run.output.records] == [
        "run.started",
        "runtime.error",
    ]
    assert client.original.closed
    assert client.calls[-3:] == ["cancel", "cleanup_session", "close"]
