"""Gateway AgentServer client telemetry proxy contracts."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    ERROR_TYPE,
    JIUWENCLAW_APP_ID,
    JIUWENCLAW_CANCELED,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_DOMAIN_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_USER_ID,
    USER_ID,
)
from jiuwenswarm.telemetry.gateway_client import wrap_gateway_agent_client


class _FakeClient(AgentServerClient):
    def __init__(self) -> None:
        self.response = object()
        self.chunks = [object(), object()]
        self.events: list[object] = []
        self.request_error: BaseException | None = None
        self.stream_error: BaseException | None = None
        self.unary_gate: asyncio.Event | None = None
        self.stream_gate: asyncio.Event | None = None
        self.unary_started = asyncio.Event()
        self.stream_started = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.server_push_handler: Any | None = None

    async def connect(self, uri: str) -> None:
        self.events.append(("connect", uri))

    async def disconnect(self) -> None:
        self.events.append("disconnect")

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        self.events.append(("config", config, env))

    async def send_request(self, envelope: E2AEnvelope) -> Any:
        self.events.append(("unary", envelope))
        self.unary_started.set()
        if self.unary_gate is not None:
            await self.unary_gate.wait()
        if self.request_error is not None:
            raise self.request_error
        return self.response

    async def send_request_stream(
        self,
        envelope: E2AEnvelope,
    ) -> AsyncIterator[Any]:
        self.events.append(("stream", envelope))
        self.stream_started.set()
        try:
            yield self.chunks[0]
            if self.stream_gate is not None:
                await self.stream_gate.wait()
            if self.stream_error is not None:
                raise self.stream_error
            yield self.chunks[1]
        finally:
            self.stream_closed.set()

    def set_server_push_handler(self, handler: Any) -> None:
        self.server_push_handler = handler


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemorySpanExporter, SimpleNamespace]:
    from jiuwenswarm.telemetry import gateway_client as gateway_client_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = SimpleNamespace(
        is_unified_active=lambda: True,
        tracer_provider=provider,
    )
    monkeypatch.setattr(gateway_client_module, "_get_runtime", lambda: runtime)
    return exporter, runtime


def _envelope(*, stream: bool = False) -> E2AEnvelope:
    return E2AEnvelope(
        request_id="request-1",
        channel="web",
        session_id="session-1",
        method="chat.send",
        is_stream=stream,
        channel_context={
            "business": "preserved",
            "user_id": "user-1",
            "domain_id": "domain-1",
            "app_id": "app-1",
        },
    )


def _only_span(exporter: InMemorySpanExporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


@pytest.mark.asyncio
async def test_unary_delegates_and_emits_client_span_with_w3c_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    wrapped = wrap_gateway_agent_client(
        inner,
        target_uri="ws://agent.test:9766/ws",
    )
    envelope = _envelope()

    response = await wrapped.send_request(envelope)

    assert response is inner.response
    assert envelope.channel_context["business"] == "preserved"
    assert envelope.channel_context["traceparent"]
    span = _only_span(exporter)
    assert span.name == "jiuwenclaw.gateway.agent.request"
    assert span.kind is SpanKind.CLIENT
    assert span.status.status_code is StatusCode.OK
    assert span.attributes == {
        JIUWENCLAW_REQUEST_ID: "request-1",
        JIUWENCLAW_CHANNEL_ID: "web",
        JIUWENCLAW_SESSION_ID: "session-1",
        "jiuwenclaw.req.method": "chat.send",
        "jiuwenclaw.stream": False,
        "server.address": "agent.test",
        "server.port": 9766,
        "network.protocol.name": "websocket",
        USER_ID: "user-1",
        JIUWENCLAW_USER_ID: "user-1",
        DOMAIN_ID: "domain-1",
        JIUWENCLAW_DOMAIN_ID: "domain-1",
        APP_ID: "app-1",
        JIUWENCLAW_APP_ID: "app-1",
    }


@pytest.mark.asyncio
async def test_proxy_transparently_delegates_lifecycle_and_guards_double_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime(monkeypatch)
    inner = _FakeClient()
    wrapped = wrap_gateway_agent_client(inner, target_uri=None)

    await wrapped.connect("opaque://target")
    wrapped.set_or_update_server_config(
        config={"model": "test"},
        env={"TOKEN": "redacted"},
    )
    await wrapped.disconnect()

    assert inner.events == [
        ("connect", "opaque://target"),
        ("config", {"model": "test"}, {"TOKEN": "redacted"}),
        "disconnect",
    ]
    assert wrap_gateway_agent_client(wrapped, target_uri="ws://ignored:1") is wrapped


def test_message_handler_registers_server_push_through_telemetry_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime(monkeypatch)
    inner = _FakeClient()
    wrapped = wrap_gateway_agent_client(inner, target_uri="ws://agent.test/ws")
    handler = object.__new__(MessageHandler)
    handler.agent_client = wrapped

    async def receive_push(_wire: dict[str, object]) -> None:
        return None

    handler._handle_agent_server_push = receive_push
    handler.set_outbound_pipeline(object())

    assert inner.server_push_handler is receive_push

@pytest.mark.asyncio
async def test_extension_target_filters_absent_server_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    wrapped = wrap_gateway_agent_client(_FakeClient(), target_uri=None)

    await wrapped.send_request(_envelope())

    attributes = _only_span(exporter).attributes
    assert "server.address" not in attributes
    assert "server.port" not in attributes
    assert "network.protocol.name" not in attributes


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_fails", [False, True])
async def test_inactive_or_unavailable_runtime_is_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
    lookup_fails: bool,
) -> None:
    from jiuwenswarm.telemetry import gateway_client as gateway_client_module

    inner = _FakeClient()
    envelope = _envelope()
    original_context = dict(envelope.channel_context)
    if lookup_fails:
        def _raise_lookup() -> None:
            raise RuntimeError("runtime unavailable")

        monkeypatch.setattr(gateway_client_module, "_get_runtime", _raise_lookup)
    else:
        monkeypatch.setattr(
            gateway_client_module,
            "_get_runtime",
            lambda: SimpleNamespace(
                is_unified_active=lambda: False,
                tracer_provider=None,
            ),
        )

    response = await wrap_gateway_agent_client(
        inner,
        target_uri="ws://agent.test:9766/ws",
    ).send_request(envelope)

    assert response is inner.response
    assert envelope.channel_context == original_context


@pytest.mark.asyncio
async def test_runtime_without_provider_is_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry import gateway_client as gateway_client_module

    inner = _FakeClient()
    envelope = _envelope()
    original_context = dict(envelope.channel_context)
    monkeypatch.setattr(
        gateway_client_module,
        "_get_runtime",
        lambda: SimpleNamespace(
            is_unified_active=lambda: True,
            tracer_provider=None,
        ),
    )

    response = await wrap_gateway_agent_client(inner).send_request(envelope)

    assert response is inner.response
    assert envelope.channel_context == original_context


@pytest.mark.asyncio
async def test_unary_records_and_reraises_same_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    error = RuntimeError("unary failed")
    inner.request_error = error

    with pytest.raises(RuntimeError) as raised:
        await wrap_gateway_agent_client(inner).send_request(_envelope())

    assert raised.value is error
    span = _only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes[ERROR_TYPE] == "RuntimeError"
    assert [event.name for event in span.events] == ["exception"]


@pytest.mark.asyncio
async def test_unary_cancellation_marks_and_ends_span_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    inner.unary_gate = asyncio.Event()
    task = asyncio.create_task(
        wrap_gateway_agent_client(inner).send_request(_envelope())
    )
    await inner.unary_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    span = _only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes[ERROR_TYPE] == "CancelledError"
    assert span.attributes[JIUWENCLAW_CANCELED] is True


@pytest.mark.asyncio
async def test_stream_span_remains_open_until_normal_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    inner.stream_gate = asyncio.Event()
    stream = wrap_gateway_agent_client(inner).send_request_stream(
        _envelope(stream=True)
    )

    assert await anext(stream) is inner.chunks[0]
    assert exporter.get_finished_spans() == ()
    inner.stream_gate.set()
    assert [chunk async for chunk in stream] == [inner.chunks[1]]

    span = _only_span(exporter)
    assert span.status.status_code is StatusCode.OK
    assert span.attributes["jiuwenclaw.stream"] is True


@pytest.mark.asyncio
async def test_stream_records_and_reraises_same_inner_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    error = LookupError("stream failed")
    inner.stream_error = error
    stream = wrap_gateway_agent_client(inner).send_request_stream(
        _envelope(stream=True)
    )
    assert await anext(stream) is inner.chunks[0]

    with pytest.raises(LookupError) as raised:
        await anext(stream)

    assert raised.value is error
    span = _only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes[ERROR_TYPE] == "LookupError"


@pytest.mark.asyncio
async def test_stream_consumer_cancellation_closes_inner_and_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    inner.stream_gate = asyncio.Event()
    stream = wrap_gateway_agent_client(inner).send_request_stream(
        _envelope(stream=True)
    )
    assert await anext(stream) is inner.chunks[0]
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert inner.stream_closed.is_set()
    span = _only_span(exporter)
    assert span.attributes[JIUWENCLAW_CANCELED] is True
    assert span.status.status_code is StatusCode.ERROR


@pytest.mark.asyncio
async def test_stream_early_break_then_aclose_ends_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    stream = wrap_gateway_agent_client(inner).send_request_stream(
        _envelope(stream=True)
    )

    async for chunk in stream:
        assert chunk is inner.chunks[0]
        break
    assert exporter.get_finished_spans() == ()
    await stream.aclose()
    await stream.aclose()

    assert inner.stream_closed.is_set()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_started_stream_destruction_releases_inner_and_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, _runtime_obj = _runtime(monkeypatch)
    inner = _FakeClient()
    stream = wrap_gateway_agent_client(inner).send_request_stream(
        _envelope(stream=True)
    )
    assert await anext(stream) is inner.chunks[0]

    del stream
    gc.collect()
    for _ in range(5):
        if inner.stream_closed.is_set():
            break
        await asyncio.sleep(0)

    assert inner.stream_closed.is_set()
    assert len(exporter.get_finished_spans()) == 1
