# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for session-complete request usage projections."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.observability.config import session_database_path
from jiuwenswarm.observability.models import TraceRecordData
from jiuwenswarm.observability.store import AsyncTrajectoryReader, TrajectoryStore


def _record(
    session_id: str,
    trace_id: str,
    span_id: str,
    inference_id: str,
    subject_id: str,
    start: int,
    input_tokens: int,
    *,
    span_name: str = "llm.call",
) -> TraceRecordData:
    attributes = {
        "openjiuwen.inference.id": inference_id,
        "openjiuwen.execution.subject.id": subject_id,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": 1,
        "gen_ai.usage.cache_read.input_tokens": input_tokens // 2,
        "gen_ai.usage.cache_tokens": 999998,
        "gen_ai.usage.total_tokens": 999999,
    }
    raw_json = json.dumps({
        "resourceSpans": [{
            "resource": {},
            "scopeSpans": [{
                "scope": {},
                "spans": [{
                    "traceId": trace_id,
                    "spanId": span_id,
                    "name": span_name,
                    "startTimeUnixNano": str(start),
                    "endTimeUnixNano": str(start + 1),
                    "attributes": [
                        {"key": key, "value": {"intValue": str(value)}}
                        if isinstance(value, int)
                        else {"key": key, "value": {"stringValue": value}}
                        for key, value in attributes.items()
                    ],
                }],
            }],
        }],
    }, separators=(",", ":")).encode()
    return TraceRecordData.from_core_record(SimpleNamespace(
        raw_json=raw_json,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        start_time_unix_nano=start,
        end_time_unix_nano=start + 1,
        session_id=session_id,
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
    ))


@pytest.mark.asyncio
async def test_session_usage_partitions_subjects_and_uses_physical_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([
            _record("session-1", "1" * 32, "1" * 16, "shared", "main", 10, 10),
            _record("session-1", "2" * 32, "2" * 16, "shared", "subagent:one", 20, 20),
            _record("session-1", "1" * 32, "3" * 16, "main-next", "main", 30, 5),
            _record(
                "session-1",
                "1" * 32,
                "4" * 16,
                "main-next",
                "main",
                31,
                999,
                span_name="llm.reasoning",
            ),
        ])
    finally:
        store.close()

    items, _epoch = await AsyncTrajectoryReader(database_path).get_session_request_usage(
        "session-1"
    )

    by_identity = {
        (item["trace_id"], item["inference_id"]): item for item in items
    }
    assert len(by_identity) == 3
    assert by_identity[("1" * 32, "shared")]["usage"]["total"] == 11
    assert by_identity[("1" * 32, "shared")]["usage"]["cacheRead"] == 5
    assert by_identity[("1" * 32, "main-next")]["cumulative_usage"]["input"] == 15
    assert by_identity[("2" * 32, "shared")]["cumulative_usage"]["input"] == 20


@pytest.mark.asyncio
async def test_session_usage_reader_locks_are_session_isolated(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    for session_id, digit in (("session-a", "a"), ("session-b", "b")):
        store = TrajectoryStore(session_database_path(root, session_id))
        store.initialize()
        try:
            store.write_records([
                _record(session_id, digit * 32, digit * 16, digit, "main", 10, 1)
            ])
        finally:
            store.close()
    reader = AsyncTrajectoryReader(root, session_scoped=True)

    await asyncio.gather(
        reader.get_session_request_usage("session-a"),
        reader.get_session_request_usage("session-b"),
    )

    assert reader._usage_locks["session-a"] is not reader._usage_locks["session-b"]
