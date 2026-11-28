# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for upstream trace context handling."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from jiuwenswarm.runtime_trace.recorder import format_traceparent, resolve_root_context


def test_valid_traceparent_is_reused(monkeypatch) -> None:
    trace_id = "1" * 32
    parent_id = "2" * 16
    monkeypatch.setenv("TRACEPARENT", f"00-{trace_id}-{parent_id}-01")
    context = resolve_root_context()
    span_context = trace.get_current_span(context).get_span_context()
    assert span_context.is_valid
    assert format(span_context.trace_id, "032x") == trace_id
    assert format(span_context.span_id, "016x") == parent_id


def test_invalid_traceparent_generates_run_trace(monkeypatch) -> None:
    for value in (
        "",
        "not-a-trace",
        "00-xyz-1-01",
        "ff-" + "1" * 32 + "-" + "2" * 16 + "-01",
    ):
        monkeypatch.setenv("TRACEPARENT", value)
        span_context = trace.get_current_span(
            resolve_root_context()
        ).get_span_context()
        assert span_context.is_valid
        assert span_context.trace_id != 0
        assert span_context.span_id != 0
        assert span_context.is_remote is True

    monkeypatch.delenv("TRACEPARENT", raising=False)
    first = trace.get_current_span(resolve_root_context()).get_span_context()
    second = trace.get_current_span(resolve_root_context()).get_span_context()
    # Each process run gets its own trace id.
    assert first.trace_id != second.trace_id


def test_all_roots_share_one_generated_trace(monkeypatch) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.delenv("TRACEPARENT", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    context = resolve_root_context()

    startup = tracer.start_span("startup", context=context)
    spawn = tracer.start_span("worker.spawn", context=context)
    startup.end()
    spawn.end()

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert len({span.context.trace_id for span in spans}) == 1
    # The virtual parent is shared but never exported as a span.
    assert {span.name for span in spans} == {"startup", "worker.spawn"}


def test_format_traceparent_roundtrip() -> None:
    provider = TracerProvider()
    span = provider.get_tracer("test").start_span("s")
    try:
        text = format_traceparent(span.get_span_context())
        assert text.startswith("00-")
        assert len(text.split("-")) == 4
        assert format_traceparent(None) == ""
    finally:
        span.end()
