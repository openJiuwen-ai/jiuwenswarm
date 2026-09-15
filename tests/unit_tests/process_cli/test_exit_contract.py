# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-shot exit boundaries; injected clients are not real-model evidence."""

from __future__ import annotations

import asyncio
import io
import signal

import pytest

from jiuwenswarm.channels.process_cli import machine, machine_entry
from jiuwenswarm.channels.process_cli.duplex_input import (
    DuplexInputError,
    DuplexLineReader,
)
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.protocol import OneShotRunInput, decode_jsonl
from tests.unit_tests.process_cli.test_duplex_control import (
    RunHarness,
    _card,
    _event,
)
from tests.unit_tests.process_cli.test_machine import FakeClient
from tests.unit_tests.process_cli.test_machine_entry import (
    _document,
    _install_runner,
    _isolated_stdio,
    _success,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_start", "during_stream", "after_answer"])
async def test_clean_eof_allows_execution_without_pending_interaction(
    when: str,
) -> None:
    run = RunHarness()
    if when == "before_start":
        run.reader.send(None)
    run.start()
    try:
        await run.output.next_event("run.started")
        if when == "during_stream":
            run.reader.send(None)
        if when == "after_answer":
            run.client.original.emit(_card())
            notice = await run.card()
            _, answer = await run.answer(notice)
            run.reader.send(None)
            answer.emit(None)
        await asyncio.sleep(0.01)
        assert run.reader.eof_seen
        assert not run.task.done()
        run.client.original.emit(_event("chat.final", content="finished after EOF"))
        run.client.original.emit(None)
        result = await run.finish()
        assert result.status == "completed"
        assert result.exit_code == 0
        assert run.client.cancel_request is None
    finally:
        await run.cleanup_test()


@pytest.mark.asyncio
async def test_interaction_after_clean_eof_fails_without_approval() -> None:
    run = RunHarness()
    run.reader.send(None)
    run.start()
    try:
        await run.output.next_event("run.started")
        await asyncio.sleep(0.01)
        run.client.original.emit(_card())
        result = await run.finish()
        assert result.error.code == "INPUT_CLOSED"
        assert result.exit_code == 130
        assert run.client.answer_inputs == []
        assert run.client.cancel_request.session_id == "runtime-session"
    finally:
        await run.cleanup_test()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["suspended", "completed", None])
@pytest.mark.parametrize("output_stream", ["original", "answer"])
async def test_empty_interrupted_segment_uses_runtime_outcome(
    completion: str | None, output_stream: str
) -> None:
    run = RunHarness()
    run.client.original.emit(_event("chat.delta", delta="before the question"))
    run.client.original.emit(_card())
    run.start()
    try:
        _, answer = await run.answer(await run.card())
        answer.emit(_event("runtime.accepted"))
        target = run.client.original if output_stream == "original" else answer
        target.emit(
            _event("chat.tool_result", tool_name="write_file", result="success")
        )
        event = _event("chat.final", content="", final_mode="patch_segment")
        event.runtime_completion = completion
        target.emit(event)
        run.client.original.emit(None)
        answer.emit(None)
        result = await run.finish()
        if completion != "suspended":
            assert result.status == "completed"
            assert run.client.cancel_request is None
        else:
            assert result.status == "failed"
            assert result.error.code == "INCOMPLETE_RUN"
            assert result.exit_code == 1
            assert run.client.cancel_request.session_id == "runtime-session"
    finally:
        await run.cleanup_test()


@pytest.mark.asyncio
async def test_old_suspended_tail_cannot_complete_an_ack_only_answer() -> None:
    run = RunHarness()
    run.client.original.emit(_card())
    run.start()
    try:
        _, answer = await run.answer(await run.card())
        tail = _event("chat.final", content="", final_mode="patch_segment")
        tail.runtime_completion = "suspended"
        run.client.original.emit(tail)
        run.client.original.emit(None)
        answer.emit(_event("runtime.accepted"))
        answer.emit(None)
        result = await run.finish()
        assert result.error.code == "INCOMPLETE_RUN"
    finally:
        await run.cleanup_test()


@pytest.mark.asyncio
async def test_noninteractive_runtime_error_does_not_wait_for_stream_eof() -> None:
    class ErrorClient(FakeClient):
        async def stream(self, request):
            self.request = request
            yield _event("chat.error", code="MODEL_FAILED", message="failed")
            pytest.fail("A fatal Runtime error must trigger cleanup immediately")

    client = ErrorClient()
    result = await machine.run_machine(
        OneShotRunInput(input="hello"),
        OneShotWriter(io.StringIO(), request_id="failure"),
        client_factory=lambda: client,
    )
    assert result.error.code == "MODEL_FAILED"
    assert client.calls[-3:] == ["cancel", "cleanup_session", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_dependency_system_exit_is_not_a_successful_command(
    exit_code: int,
) -> None:
    client = FakeClient()
    client.errors["start"] = SystemExit(exit_code)
    result = await machine.run_machine(
        OneShotRunInput(input="hello"),
        OneShotWriter(io.StringIO(), request_id="dependency-exit"),
        client_factory=lambda: client,
    )
    assert result.status == "failed"
    assert result.exit_code == 1
    assert client.calls == ["start", "close"]


def test_asyncio_shutdown_failure_cannot_publish_success(monkeypatch) -> None:
    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("private-shutdown-diagnostic")

    async def run(_run_input, writer):
        asyncio.create_task(background())
        await asyncio.sleep(0)
        return _success(writer)

    _install_runner(monkeypatch, run)
    output = _isolated_stdio(monkeypatch, _document())
    assert machine_entry.execute_source("-") == 1
    final = decode_jsonl(output.getvalue())[-1]
    assert final.error.code == "SHUTDOWN_FAILED"
    assert final.error.details["cleanup_errors"] == ("asyncio_shutdown",)
    assert "private-shutdown-diagnostic" not in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"a" * 32, b"line1\nline2", "中文".encode()])
async def test_document_reader_preserves_bytes_until_eof(content: bytes) -> None:
    source = io.BytesIO(content)
    async with DuplexLineReader(source, max_line_bytes=32) as reader:
        assert await reader.read_document() == content
        assert reader.buffered_bytes == 0
    assert not source.closed


@pytest.mark.asyncio
async def test_document_reader_enforces_its_whole_document_limit() -> None:
    async with DuplexLineReader(io.BytesIO(b"a\n" * 17), max_line_bytes=32) as reader:
        with pytest.raises(DuplexInputError, match="byte limit"):
            await reader.read_document()
        assert reader.buffered_bytes == 0
        assert reader.closed


def test_final_output_value_error_never_escapes_or_retries(monkeypatch) -> None:
    class ClosedOutput(io.StringIO):
        def write(self, _value):
            raise ValueError("closed data pipe")

    async def run(_run_input, writer):
        return _success(writer)

    _install_runner(monkeypatch, run)
    _isolated_stdio(monkeypatch, _document())
    import sys

    monkeypatch.setattr(sys, "stdout", ClosedOutput())
    assert machine_entry.execute_source("-") == 1


def test_first_runtime_error_survives_asyncio_shutdown_failure(monkeypatch) -> None:
    from dataclasses import replace
    from jiuwenswarm.channels.process_cli.protocol import RuntimeErrorInfo

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("shutdown failure")

    async def run(_run_input, writer):
        asyncio.create_task(background())
        await asyncio.sleep(0)
        return replace(
            _success(writer),
            status="timed_out",
            exit_code=124,
            error=RuntimeErrorInfo(code="TIMEOUT", message="expired"),
        )

    _install_runner(monkeypatch, run)
    output = _isolated_stdio(monkeypatch, _document())
    assert machine_entry.execute_source("-") == 124
    result = decode_jsonl(output.getvalue())[-1]
    assert result.error.code == "TIMEOUT"
    assert result.error.details["cleanup_errors"] == ("asyncio_shutdown",)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_during_cleanup", [False, True])
async def test_signals_do_not_interrupt_runtime_close(
    first_during_cleanup: bool, monkeypatch
) -> None:
    from jiuwenswarm.channels.process_cli.machine_signals import command_signals

    handlers = {}
    monkeypatch.setattr(
        signal, "signal", lambda number, handler: handlers.setdefault(number, handler)
    )
    client = FakeClient()
    client.hang_at = "runtime_close" if first_during_cleanup else "stream"
    writer = OneShotWriter(io.StringIO(), request_id="signals")
    with command_signals() as signals:
        task = asyncio.create_task(
            signals.run(
                machine.run_machine(
                    OneShotRunInput(input="hello"),
                    writer,
                    client_factory=lambda: client,
                )
            )
        )
        await asyncio.wait_for(client.blocked.wait(), timeout=1)
        if not first_during_cleanup:
            client.hang_at = "runtime_close"
            client.blocked.clear()
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            await asyncio.wait_for(client.blocked.wait(), timeout=1)
        handlers[signal.SIGINT](signal.SIGINT, None)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert not task.done(), "Runtime close was abandoned by a signal"
        client.release.set()
        result = signals.finalize(await asyncio.wait_for(task, timeout=1))
    assert result.status == "cancelled"
    assert result.exit_code == 130
    assert "cleanup_errors" not in result.error.details
    assert client.calls[-1] == "close"


def test_signal_scope_restores_handlers_and_late_signal_keeps_published_outcome() -> (
    None
):
    from jiuwenswarm.channels.process_cli.machine_signals import command_signals

    numbers = [
        getattr(signal, name)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK")
        if hasattr(signal, name)
    ]
    before = {number: signal.getsignal(number) for number in numbers}
    writer = OneShotWriter(io.StringIO(), request_id="settled")
    with command_signals() as signals:
        result = signals.finalize(_success(writer))
        signals.interrupt(signal.SIGTERM, None)
        assert not signals.interrupted
        assert signals.finalize(result) is result
    assert {number: signal.getsignal(number) for number in numbers} == before


@pytest.mark.asyncio
async def test_control_cleanup_failure_is_reported_even_without_client() -> None:
    from unittest.mock import AsyncMock, Mock

    control = Mock()
    control.stop_input = AsyncMock(side_effect=RuntimeError("reader close failed"))

    def create_client():
        raise RuntimeError("startup failure")

    result = await machine.run_machine(
        OneShotRunInput(input="hello"),
        OneShotWriter(io.StringIO(), request_id="no-client"),
        client_factory=create_client,
        control=control,
    )
    assert result.error.code == "RUNTIME_FAILED"
    assert result.error.details["cleanup_errors"] == ("control_input",)
    control.stop_input.assert_awaited_once()
