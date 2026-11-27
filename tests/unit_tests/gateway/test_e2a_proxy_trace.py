"""E2A proxy entries must keep the caller trace from the WS handshake."""

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from jiuwenswarm.gateway import telemetry
from jiuwenswarm.gateway.routing.e2a_proxy import _bind_proxy_request_trace

TRACE_HEX = "6a945399111141118111111111111111"
PARENT_HEX = "b" * 16
TRACEPARENT = f"00-{TRACE_HEX}-{PARENT_HEX}-01"


@pytest.mark.parametrize("header_attr", ["headers", "request_headers"])
def test_proxy_trace_reads_handshake_headers(monkeypatch, header_attr):
    monkeypatch.setattr(telemetry, "_runtime", None)
    runtime = telemetry.configure_gateway_telemetry(
        {"telemetry": {"enabled": True, "exporter": "none"}},
    )
    exporter = InMemorySpanExporter()
    runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    ws = SimpleNamespace(**{header_attr: {"traceparent": TRACEPARENT}})
    channel = SimpleNamespace(channel_id="tui")

    with _bind_proxy_request_trace(
        channel, ws, "req", "chat.send", None, "u1",
    ) as operation:
        assert operation is not None
        assert operation.trace_id == TRACE_HEX
        assert telemetry.current_request_trace() is operation

    span = next(s for s in exporter.get_finished_spans() if s.name == "gw.session.message")
    assert span.context.trace_id == int(TRACE_HEX, 16)
    assert span.parent.span_id == int(PARENT_HEX, 16)
