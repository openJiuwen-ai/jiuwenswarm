# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-repository regression for Core processor to Swarm SQLite delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_span_in_context

from jiuwenswarm.observability.config import TrajectoryStoreSettings
from jiuwenswarm.observability.sink import TrajectoryRecordSink
from jiuwenswarm.observability.store import AsyncTrajectoryReader
from openjiuwen.extensions.observability.span_record_processor import SpanRecordProcessor
from openjiuwen.extensions.observability.trajectory_events import (
    emit_context_window_commit,
)


def _settings(database_path: Path) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=True,
        database_path=database_path,
        retention_days=7,
        queue_size=16,
        batch_size=8,
        flush_interval_ms=10,
        poll_interval_ms=2000,
    )


def _span(record: dict[str, Any]) -> dict[str, Any]:
    return record["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attribute(span: dict[str, Any], key: str) -> Any:
    for attribute in span.get("attributes", []):
        if attribute.get("key") != key:
            continue
        value = attribute.get("value", {})
        for value_key in (
            "stringValue",
            "boolValue",
            "intValue",
            "doubleValue",
        ):
            if value_key in value:
                return value[value_key]
    return None


async def _wait_for_record(
    reader: AsyncTrajectoryReader,
    session_id: str,
    trace_id: str,
    span_id: str,
    *,
    lifecycle: str,
    minimum_revision: int,
) -> dict[str, Any]:
    for _attempt in range(100):
        detail = await reader.get_trace_records(
            session_id,
            trace_id,
            since_revision=0,
            limit=1000,
        )
        if detail is not None:
            matches = [
                record
                for record in detail["records"]
                if record.get("record_id") == f"{trace_id}:{span_id}"
                and record.get("lifecycle") == lifecycle
                and int(record.get("record_revision", 0)) >= minimum_revision
            ]
            if matches:
                return matches[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"timed out waiting for {span_id} lifecycle={lifecycle} revision>={minimum_revision}"
    )


@pytest.mark.asyncio
async def test_core_processor_delivers_child_then_complete_root_without_reencoding(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    sink = TrajectoryRecordSink(_settings(database_path))
    processor = SpanRecordProcessor()
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("tests.core-swarm-trajectory")

    session_id = "session-core-swarm"
    request_id = "request-core-swarm"
    sink.start()
    processor.register_consumer(sink)
    try:
        root = tracer.start_span(
            "agent.agent.session-core-swarm",
            attributes={
                "openjiuwen.trace.root": True,
                "openjiuwen.trace.schema_version": "1",
                "openjiuwen.trajectory.record.kind": "turn",
                "openjiuwen.session.id": session_id,
                "openjiuwen.request.id": request_id,
                "openjiuwen.run.id": request_id,
                "openjiuwen.agent.mode": "agent.work.normal",
            },
        )
        child = tracer.start_span(
            "llm.call",
            context=set_span_in_context(root),
            attributes={
                "openjiuwen.trace.schema_version": "1",
                "openjiuwen.trajectory.record.kind": "inference",
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}]
                ),
                "gen_ai.output.messages": json.dumps(
                    [{"role": "assistant", "parts": [{"type": "text", "content": "world"}]}]
                ),
            },
        )
        trace_id = f"{root.get_span_context().trace_id:032x}"
        child_span_id = f"{child.get_span_context().span_id:016x}"
        root_span_id = f"{root.get_span_context().span_id:016x}"

        child.end()
        root.set_attribute("openjiuwen.trace.complete", True)
        root.end()
    finally:
        processor.unregister_consumer(sink)
        assert sink.close(timeout=5) is True
        provider.shutdown()

    reader = AsyncTrajectoryReader(database_path)
    detail = await reader.get_trace_records(
        session_id,
        trace_id,
        since_revision=0,
        limit=1000,
    )

    assert detail is not None
    assert len(detail["records"]) == 2
    child_record, root_record = detail["records"]
    assert _span(child_record)["spanId"] == child_span_id
    assert _span(root_record)["spanId"] == root_span_id
    assert _span(child_record)["parentSpanId"] == root_span_id
    assert _attribute(_span(root_record), "openjiuwen.trace.complete") is True

    raw_root = await reader.get_raw_record(session_id, trace_id, root_span_id)
    assert raw_root is not None
    assert json.loads(raw_root) == root_record["otlp"]


@pytest.mark.asyncio
async def test_core_snapshot_is_visible_before_end_and_finalizes_same_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory-live.sqlite3"
    sink = TrajectoryRecordSink(_settings(database_path))
    processor = SpanRecordProcessor()
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("tests.core-swarm-live-trajectory")
    reader = AsyncTrajectoryReader(database_path)

    session_id = "session-core-swarm-live"
    root = None
    child = None
    sink.start()
    processor.register_consumer(sink)
    try:
        routing = {
            "openjiuwen.trace.schema_version": "1",
            "openjiuwen.session.id": session_id,
            "gen_ai.conversation.id": session_id,
            "openjiuwen.request.id": "request-live",
            "openjiuwen.run.id": "run-live",
            "openjiuwen.agent.mode": "agent.work.normal",
        }
        root = tracer.start_span(
            "agent.agent.session-core-swarm-live",
            attributes={
                **routing,
                "openjiuwen.trace.root": True,
                "openjiuwen.trajectory.record.kind": "turn",
            },
        )
        child = tracer.start_span(
            "llm.call",
            context=set_span_in_context(root),
            attributes={
                **routing,
                "openjiuwen.trajectory.record.kind": "inference",
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}]
                ),
            },
        )
        trace_id = f"{root.get_span_context().trace_id:032x}"
        child_span_id = f"{child.get_span_context().span_id:016x}"

        running = await _wait_for_record(
            reader,
            session_id,
            trace_id,
            child_span_id,
            lifecycle="running",
            minimum_revision=1,
        )
        assert "endTimeUnixNano" not in _span(running)
        assert await reader.get_raw_record(session_id, trace_id, child_span_id) is None

        child.set_attribute(
            "gen_ai.output.messages",
            json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": "partial"}]}]
            ),
        )
        processor.publish_snapshot(child, "stream_chunk")
        updated = await _wait_for_record(
            reader,
            session_id,
            trace_id,
            child_span_id,
            lifecycle="running",
            minimum_revision=2,
        )
        assert _attribute(_span(updated), "gen_ai.output.messages") is not None

        child.end()
        child = None
        final = await _wait_for_record(
            reader,
            session_id,
            trace_id,
            child_span_id,
            lifecycle="final",
            minimum_revision=3,
        )
        assert _span(final)["endTimeUnixNano"]
        assert final["record_id"] == running["record_id"] == updated["record_id"]
        raw_child = await reader.get_raw_record(session_id, trace_id, child_span_id)
        assert raw_child is not None
        assert json.loads(raw_child) == final["otlp"]
    finally:
        if child is not None:
            child.end()
        if root is not None:
            root.end()
        processor.unregister_consumer(sink)
        assert sink.close(timeout=5) is True
        provider.shutdown()


@pytest.mark.asyncio
async def test_native_context_event_is_queryable_before_parent_request_ends(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory-native-event.sqlite3"
    sink = TrajectoryRecordSink(_settings(database_path))
    processor = SpanRecordProcessor()
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("tests.core-swarm-native-event")
    reader = AsyncTrajectoryReader(database_path)

    session_id = "session-native-event"
    parent = None
    sink.start()
    processor.register_consumer(sink)
    try:
        parent = tracer.start_span(
            "llm.call",
            attributes={
                "openjiuwen.trace.schema_version": "2",
                "openjiuwen.session.id": session_id,
                "gen_ai.conversation.id": session_id,
                "openjiuwen.request.id": "request-native-event",
                "openjiuwen.run.id": "run-native-event",
                "openjiuwen.agent.mode": "agent.work.normal",
                "openjiuwen.execution.subject.id": "main",
                "openjiuwen.execution.subject.kind": "main_agent",
                "openjiuwen.execution.subject.session_id": session_id,
                "openjiuwen.turn.number": 1,
                "openjiuwen.step.number": 1,
            },
        )
        event = emit_context_window_commit(
            tracer=tracer,
            llm_span=parent,
            messages=[
                {
                    "message_id": "user-1",
                    "role": "user",
                    "content": "hello",
                    "metadata": {"context_message_id": "user-1"},
                }
            ],
            request_purpose="agent",
        )
        assert event is not None

        trace_id = f"{parent.get_span_context().trace_id:032x}"
        event_span_id = f"{event.get_span_context().span_id:016x}"
        record = await _wait_for_record(
            reader,
            session_id,
            trace_id,
            event_span_id,
            lifecycle="final",
            minimum_revision=2,
        )

        span = _span(record)
        assert _attribute(span, "openjiuwen.trajectory.schema_version") == "2"
        assert (
            _attribute(span, "openjiuwen.trajectory.event_kind")
            == "context.window.commit"
        )
        assert _attribute(span, "openjiuwen.execution.subject.id") == "main"
        payload = json.loads(_attribute(span, "openjiuwen.trajectory.payload"))
        assert payload["complete"] is True
        assert payload["messages"][0]["message_id"] == "user-1"
        # First window is the epoch baseline: no delta against a prior window.
        assert payload["transition_kind"] == "epoch_baseline"
        assert payload["delta"] == []
    finally:
        if parent is not None:
            parent.end()
        processor.unregister_consumer(sink)
        assert sink.close(timeout=5) is True
        provider.shutdown()
