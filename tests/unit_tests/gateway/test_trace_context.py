"""One trace rule for every Gateway entry and southbound client."""

from uuid import UUID

import pytest

from jiuwenswarm.gateway import telemetry
from jiuwenswarm.gateway.trace_context import (
    current_trace_id,
    outbound_trace_headers,
    resolve_trace_id,
    trace_context_from_headers,
    trace_id_from_headers,
)

HEX = "6a945399111141118111111111111111"
UUID_STR = str(UUID(hex=HEX))
TRACEPARENT = f"00-{HEX}-bbbbbbbbbbbbbbbb-01"


def _enable_trace(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    telemetry.configure_gateway_telemetry(
        {"telemetry": {"enabled": True, "exporter": "none"}},
    )


def test_trace_context_from_headers_drops_empty_and_lowercases() -> None:
    context = trace_context_from_headers({
        "Traceparent": TRACEPARENT,
        "TraceState": "vendor=value",
        "X-Trace-Id": UUID_STR,
        "trace_id": "",
        "ignored": "x",
    })
    assert context == {
        "traceparent": TRACEPARENT,
        "tracestate": "vendor=value",
        "x-trace-id": UUID_STR,
    }
    assert trace_context_from_headers(None) == {}
    assert trace_context_from_headers(object()) == {}


def test_trace_id_from_headers_prefers_explicit_then_traceparent() -> None:
    assert trace_id_from_headers({"Traceparent": TRACEPARENT}) == UUID_STR
    assert trace_id_from_headers({
        "traceparent": TRACEPARENT,
        "X-Trace-Id": "11111111-1111-4111-8111-111111111111",
    }) == "11111111-1111-4111-8111-111111111111"
    assert trace_id_from_headers({"traceparent": "00-" + "0" * 32 + "-" + "b" * 16 + "-01"}) == ""
    assert trace_id_from_headers({"traceparent": "not-a-traceparent"}) == ""
    assert trace_id_from_headers(None) == ""


def test_resolve_trace_id_prefers_explicit_and_mints() -> None:
    assert resolve_trace_id(UUID_STR) == UUID_STR
    assert resolve_trace_id("x" * 200) == "x" * 128
    minted = resolve_trace_id()
    assert UUID(minted)


def test_resolve_trace_id_uses_bound_span(monkeypatch) -> None:
    _enable_trace(monkeypatch)
    operation = telemetry.start_request_trace(
        "request",
        "chat.send",
        channel="web",
        connection_id="connection",
        trace_context={"traceparent": TRACEPARENT},
    )
    assert operation is not None
    try:
        with telemetry.bind_request_trace(operation):
            assert current_trace_id() == UUID_STR
            assert resolve_trace_id() == UUID_STR
    finally:
        operation.finish("success")


def test_outbound_headers_keep_explicit_and_current_on_the_same_key(monkeypatch) -> None:
    _enable_trace(monkeypatch)
    operation = telemetry.start_request_trace(
        "request",
        "chat.send",
        channel="web",
        connection_id="connection",
        trace_context={"traceparent": TRACEPARENT},
    )
    assert operation is not None
    explicit = "11111111-1111-4111-8111-111111111111"
    try:
        with telemetry.bind_request_trace(operation):
            inherited = outbound_trace_headers({})
            assert inherited["X-Trace-Id"] == UUID_STR
            assert inherited["traceparent"].split("-")[1] == HEX

            overridden = outbound_trace_headers({}, explicit)
            assert overridden["X-Trace-Id"] == explicit
            assert overridden["traceparent"].split("-")[1] == explicit.replace("-", "")
            assert "tracestate" not in overridden

        fallback = outbound_trace_headers({"X-Trace-Id": "opaque-caller"}, None)
        assert fallback["X-Trace-Id"] == "opaque-caller"
        assert "traceparent" not in fallback
    finally:
        operation.finish("success")
