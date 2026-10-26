from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from jiuwenswarm.telemetry.span_registry import SpanRegistryProcessor


def _remote_parent(*, trace_id: int, span_id: int):
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


@pytest.fixture
def otel_provider() -> Iterator[TracerProvider]:
    provider = TracerProvider()
    yield provider
    provider.shutdown()


def test_child_span_inherits_trace_attributes_without_overwriting_nonempty_values(
    otel_provider: TracerProvider,
) -> None:
    registry = SpanRegistryProcessor(max_spans=32, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    trace_id = 0x123
    registry.bind_trace_attributes(
        trace_id,
        {
            "jiuwenclaw.session.id": "from-trace",
            "jiuwenclaw.channel.id": "web",
        },
    )

    span = otel_provider.get_tracer("test").start_span(
        "llm.call",
        context=_remote_parent(trace_id=trace_id, span_id=0x456),
        attributes={
            "jiuwenclaw.session.id": "from-child",
            "jiuwenclaw.channel.id": "",
        },
    )

    assert span.attributes["jiuwenclaw.session.id"] == "from-child"
    assert span.attributes["jiuwenclaw.channel.id"] == "web"
    span.end()


def test_active_parent_attributes_and_public_queries_use_the_same_span_objects(
    otel_provider: TracerProvider,
) -> None:
    registry = SpanRegistryProcessor(max_spans=32, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    tracer = otel_provider.get_tracer("test")
    parent = tracer.start_span(
        "agent.worker.invoke",
        context=_remote_parent(trace_id=0xAAA, span_id=0x100),
        attributes={
            "jiuwenclaw.request.id": "r1",
            "gen_ai.span.type": "agent",
            "gen_ai.input.messages": "parent-only",
        },
    )
    child = tracer.start_span(
        "llm.call",
        context=trace.set_span_in_context(parent),
    )
    child_context = child.get_span_context()
    parent_context = parent.get_span_context()

    assert child.attributes["jiuwenclaw.request.id"] == "r1"
    assert "gen_ai.span.type" not in child.attributes
    assert "gen_ai.input.messages" not in child.attributes
    assert registry.find(child_context.trace_id, child_context.span_id) is child
    assert registry.find_latest(child_context.trace_id, name="llm.call") is child
    assert registry.find_latest(child_context.trace_id, name_prefix="agent.") is parent
    assert (
        registry.find_latest(
            child_context.trace_id,
            parent_span_id=parent_context.span_id,
        )
        is child
    )
    assert registry.find_parent(child, name_prefix="agent.") is parent
    assert registry.find_parent(parent, name_prefix="agent.") is None
    assert registry.active_count() == 2

    child.end()
    assert registry.find(child_context.trace_id, child_context.span_id) is None
    assert registry.active_count() == 1
    parent.end()
    assert registry.active_count() == 0


def test_parent_values_win_over_trace_defaults_but_not_child_values(
    otel_provider: TracerProvider,
) -> None:
    registry = SpanRegistryProcessor(max_spans=32, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    registry.bind_trace_attributes(
        0xAAA,
        {"jiuwenclaw.session.id": "trace-default"},
    )
    tracer = otel_provider.get_tracer("test")
    parent = tracer.start_span(
        "agent.invoke",
        context=_remote_parent(trace_id=0xAAA, span_id=0x100),
        attributes={"jiuwenclaw.session.id": "parent"},
    )
    child = tracer.start_span(
        "tool.search",
        context=trace.set_span_in_context(parent),
    )

    assert child.attributes["jiuwenclaw.session.id"] == "parent"
    child.end()
    parent.end()


def test_empty_parent_value_does_not_erase_nonempty_trace_default(
    otel_provider: TracerProvider,
) -> None:
    registry = SpanRegistryProcessor(max_spans=32, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    tracer = otel_provider.get_tracer("test")
    parent = tracer.start_span(
        "agent.invoke",
        context=_remote_parent(trace_id=0xAAA, span_id=0x100),
        attributes={"jiuwenclaw.session.id": ""},
    )
    registry.bind_trace_attributes(
        0xAAA,
        {"jiuwenclaw.session.id": "trace-default"},
    )
    child = tracer.start_span(
        "tool.search",
        context=trace.set_span_in_context(parent),
        attributes={"jiuwenclaw.session.id": ""},
    )

    assert child.attributes["jiuwenclaw.session.id"] == "trace-default"
    child.end()
    parent.end()


def test_on_end_removes_only_span_index_and_keeps_trace_attributes(
    otel_provider: TracerProvider,
) -> None:
    registry = SpanRegistryProcessor(max_spans=32, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    registry.bind_trace_attributes(0xABC, {"jiuwenclaw.session.id": "s1"})
    tracer = otel_provider.get_tracer("test")
    first = tracer.start_span(
        "first",
        context=_remote_parent(trace_id=0xABC, span_id=0x101),
    )
    first.end()

    second = tracer.start_span(
        "second",
        context=_remote_parent(trace_id=0xABC, span_id=0x102),
    )

    assert second.attributes["jiuwenclaw.session.id"] == "s1"
    second.end()


def test_span_capacity_evicts_oldest_active_entry(
    otel_provider: TracerProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.span_registry.time.monotonic",
        lambda: next(clock),
    )
    registry = SpanRegistryProcessor(max_spans=2, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    tracer = otel_provider.get_tracer("test")
    spans = [
        tracer.start_span(
            f"span-{index}",
            context=_remote_parent(trace_id=0x100 + index, span_id=0x10 + index),
        )
        for index in range(3)
    ]

    first_context = spans[0].get_span_context()
    assert registry.active_count() == 2
    assert registry.find(first_context.trace_id, first_context.span_id) is None
    for span in spans:
        span.end()


def test_prune_removes_expired_spans_and_trace_attributes(
    otel_provider: TracerProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10.0
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.span_registry.time.monotonic", lambda: now
    )
    registry = SpanRegistryProcessor(max_spans=8, ttl_seconds=5)
    otel_provider.add_span_processor(registry)
    registry.bind_trace_attributes(0xAAA, {"jiuwenclaw.session.id": "expired"})
    span = otel_provider.get_tracer("test").start_span(
        "old",
        context=_remote_parent(trace_id=0xBBB, span_id=0x10),
    )
    now = 16.0

    assert registry.prune() == 2
    assert registry.active_count() == 0
    fresh = otel_provider.get_tracer("test").start_span(
        "fresh",
        context=_remote_parent(trace_id=0xAAA, span_id=0x11),
    )
    assert "jiuwenclaw.session.id" not in fresh.attributes
    fresh.end()
    span.end()


def test_concurrent_traces_with_same_remote_parent_span_id_do_not_cross_talk(
    otel_provider: TracerProvider,
) -> None:
    registry = SpanRegistryProcessor(max_spans=32, ttl_seconds=60)
    otel_provider.add_span_processor(registry)
    registry.bind_trace_attributes(0xA1, {"jiuwenclaw.session.id": "s1"})
    registry.bind_trace_attributes(0xA2, {"jiuwenclaw.session.id": "s2"})
    barrier = threading.Barrier(3)
    results: dict[int, tuple[str, object]] = {}

    def create(trace_id: int) -> None:
        span = otel_provider.get_tracer("thread").start_span(
            "llm.call",
            context=_remote_parent(trace_id=trace_id, span_id=0x99),
        )
        results[trace_id] = (span.attributes["jiuwenclaw.session.id"], span)
        barrier.wait()
        barrier.wait()
        span.end()

    threads = [threading.Thread(target=create, args=(trace_id,)) for trace_id in (0xA1, 0xA2)]
    for thread in threads:
        thread.start()
    barrier.wait()

    for trace_id, expected in ((0xA1, "s1"), (0xA2, "s2")):
        value, span = results[trace_id]
        span_context = span.get_span_context()
        assert value == expected
        assert registry.find(trace_id, span_context.span_id) is span
    barrier.wait()
    for thread in threads:
        thread.join()
    assert registry.active_count() == 0
