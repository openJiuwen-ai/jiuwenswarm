"""A provider that closes a stream after the first chunks must be retried."""

from __future__ import annotations

import pytest

from openjiuwen.core.single_agent.rail.base import MODEL_VISIBLE_OUTPUT_EMITTED_KEY
from openjiuwen.harness.rails.model_anomaly_detection_rail import ModelAnomalyDetectionRail

from jiuwenswarm.server.jiuwenmate.dropped_stream_retry import (
    install_dropped_stream_retry,
    is_dropped_stream,
    request_dropped_stream_retry,
)

_CLOSED = (
    "FrameworkError: [181001] model call failed, reason: openAI API async stream error: "
    "RemoteProtocolError: peer closed connection without sending complete message body "
    "(incomplete chunked read)"
)


class _Ctx:
    def __init__(self, exception, *, attempt=0, visible=True, already=False):
        self.exception = exception
        self.retry_attempt = attempt
        self.extra = {MODEL_VISIBLE_OUTPUT_EMITTED_KEY: visible}
        self._retry_request = object() if already else None
        self.delays: list[float] = []

    def request_retry(self, delay_seconds: float = 0.0) -> None:
        self.delays.append(delay_seconds)
        self._retry_request = object()


def test_incomplete_chunked_read_is_a_dropped_stream():
    assert is_dropped_stream(RuntimeError(_CLOSED))


def test_connection_error_is_a_dropped_stream():
    assert is_dropped_stream(RuntimeError("APIConnectionError: Connection error."))


def test_quota_and_context_errors_are_not_retried_as_drops():
    assert not is_dropped_stream(RuntimeError("Error code: 402 - insufficient_quota"))
    assert not is_dropped_stream(RuntimeError("maximum context length exceeded"))


def test_visible_incomplete_stream_is_retried_twice():
    first = _Ctx(RuntimeError(_CLOSED), attempt=0, visible=True)
    assert request_dropped_stream_retry(first) is True
    assert first.delays == [1.0]

    second = _Ctx(RuntimeError(_CLOSED), attempt=1, visible=True)
    assert request_dropped_stream_retry(second) is True
    assert second.delays == [2.0]

    stopped = _Ctx(RuntimeError(_CLOSED), attempt=2, visible=True)
    assert request_dropped_stream_retry(stopped) is False
    assert stopped.delays == []


def test_existing_retry_request_is_left_alone():
    ctx = _Ctx(RuntimeError(_CLOSED), already=True)
    assert request_dropped_stream_retry(ctx) is False
    assert ctx.delays == []


def test_ordinary_model_error_is_not_a_dropped_stream():
    ctx = _Ctx(RuntimeError("Error code: 400 - invalid request"))
    assert request_dropped_stream_retry(ctx) is False


@pytest.mark.asyncio
async def test_installed_rail_retries_a_closed_stream_after_visible_output():
    install_dropped_stream_retry()
    install_dropped_stream_retry()
    rail = ModelAnomalyDetectionRail()
    ctx = _Ctx(RuntimeError(_CLOSED), visible=True)

    await ModelAnomalyDetectionRail.on_model_exception(rail, ctx)

    assert ctx.delays == [1.0]
