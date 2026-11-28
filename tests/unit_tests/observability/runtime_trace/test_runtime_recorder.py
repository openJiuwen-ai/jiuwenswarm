# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for RuntimeRecorder semantics."""

from __future__ import annotations

import time

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from jiuwenswarm.runtime_trace.clock import ClockBase
from jiuwenswarm.runtime_trace.recorder import (
    RuntimeRecorder,
    SpanKinds,
    resolve_root_context,
)


@pytest.fixture()
def traced():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    kinds = SpanKinds()
    recorder = RuntimeRecorder(
        tracer=tracer,
        kinds=kinds,
        clock=ClockBase(),
        base_attrs={
            "runtime.scope": "runtime",
            "runtime.run_id": "a" * 32,
            "runtime.role": "front",
            "runtime.pid": 1234,
        },
        root_context=resolve_root_context(),
    )
    return recorder, kinds, exporter


def _attrs(span) -> dict:
    return dict(span.attributes or {})


def test_root_and_child_parent_relationship(traced) -> None:
    recorder, _kinds, exporter = traced
    root = recorder.start("startup", level="root")
    child = recorder.start("startup.phase.x", parent=root, level="normal")
    recorder.end(child, "success")
    recorder.end(root, "success")
    spans = exporter.get_finished_spans()
    root_span = next(span for span in spans if span.name == "startup")
    child_span = next(span for span in spans if span.name == "startup.phase.x")
    assert child_span.parent.span_id == root_span.context.span_id
    assert child_span.context.trace_id == root_span.context.trace_id
    assert _attrs(root_span)["runtime.outcome"] == "success"


def test_explicit_times_are_used(traced) -> None:
    recorder, _kinds, exporter = traced
    clock = recorder._clock
    start_mono = time.monotonic_ns()
    end_mono = start_mono + 1_000_000
    handle = recorder.start("task.x", mono_start=start_mono)
    recorder.end(handle, "slow", mono_end=end_mono)
    span = exporter.get_finished_spans()[0]
    assert span.start_time == clock.to_unix_nano(start_mono)
    assert span.end_time == clock.to_unix_nano(end_mono)


def test_outcome_is_overwritten_by_recorder(traced) -> None:
    recorder, _kinds, exporter = traced
    handle = recorder.start(
        "task.x", attributes={"runtime.outcome": "success"}
    )
    recorder.end(handle, "error", attributes={"runtime.outcome": "success"})
    span = exporter.get_finished_spans()[0]
    assert _attrs(span)["runtime.outcome"] == "error"


def test_invalid_outcome_becomes_error(traced) -> None:
    recorder, _kinds, exporter = traced
    handle = recorder.start("task.x")
    recorder.end(handle, "bogus")
    assert _attrs(exporter.get_finished_spans()[0])["runtime.outcome"] == "error"


@pytest.mark.parametrize(
    "key,value",
    [
        ("runtime.reason_code", "not_a_code"),
        ("runtime.launch", "XYZ"),
        ("runtime.worker_instance", "bad id!"),
        ("runtime.attempt", 0),
        ("runtime.worker_pid", 0),
        ("session.id", "forbidden"),
        ("runtime.exit_code", "string"),
    ],
)
def test_invalid_attributes_are_dropped(traced, key: str, value: object) -> None:
    recorder, _kinds, exporter = traced
    handle = recorder.start("task.x", attributes={key: value})
    recorder.end(handle, "success")
    assert key not in _attrs(exporter.get_finished_spans()[0])


def test_valid_whitelisted_attributes_are_kept(traced) -> None:
    recorder, _kinds, exporter = traced
    handle = recorder.start(
        "worker.exited",
        level="terminal",
        attributes={
            "runtime.reason_code": "process_exit",
            "runtime.exit_code": 2,
            "runtime.worker_pid": 99,
        },
    )
    recorder.end(handle, "success")
    attrs = _attrs(exporter.get_finished_spans()[0])
    assert attrs["runtime.reason_code"] == "process_exit"
    assert attrs["runtime.exit_code"] == 2
    assert attrs["runtime.worker_pid"] == 99


def test_span_kinds_registration_and_outcome(traced) -> None:
    recorder, kinds, exporter = traced
    handle = recorder.start("worker.spawn", level="event")
    span_id = format(handle.span.get_span_context().span_id, "016x")
    # Registered until the span ends, then consumed by end() itself.
    recorder.end(handle, "success")
    assert kinds.size() == 0
    assert exporter.get_finished_spans()[0].name == "worker.spawn"


def test_end_is_idempotent(traced) -> None:
    recorder, _kinds, exporter = traced
    handle = recorder.start("task.x")
    recorder.end(handle, "success")
    recorder.end(handle, "success")
    assert len(exporter.get_finished_spans()) == 1


def test_event_requires_open_root(traced) -> None:
    recorder, _kinds, exporter = traced
    root = recorder.start("startup", level="root")
    recorder.event(root, "startup.warmup_retry")
    recorder.end(root, "success")
    recorder.event(root, "startup.degraded")  # dropped, no exception
    span = exporter.get_finished_spans()[0]
    assert [event.name for event in span.events] == ["startup.warmup_retry"]


def test_worker_meta_updates_base_attrs(traced) -> None:
    recorder, _kinds, exporter = traced
    recorder.set_worker_instance("worker_1")
    recorder.set_worker_generation(3)
    handle = recorder.start("task.x")
    recorder.end(handle, "success")
    attrs = _attrs(exporter.get_finished_spans()[0])
    assert attrs["runtime.worker_instance"] == "worker_1"
    assert attrs["runtime.generation"] == 3


def test_base_attrs_win_over_instrumentation(traced) -> None:
    recorder, _kinds, exporter = traced
    handle = recorder.start("task.x", attributes={"runtime.pid": 99999})
    recorder.end(handle, "success")
    # runtime.pid is not in the whitelist, so it is dropped rather than retried.
    assert _attrs(exporter.get_finished_spans()[0])["runtime.pid"] == 1234


def test_public_methods_never_raise_on_bad_handle(traced) -> None:
    recorder, _kinds, _exporter = traced
    recorder.end(None, "success")
    recorder.event(None, "x")
    recorder.end(recorder.start("task.x"), "success")
