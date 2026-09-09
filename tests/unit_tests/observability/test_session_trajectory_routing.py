# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for per-session trajectory database routing."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    session_database_path,
)
from jiuwenswarm.observability.models import TraceRecordData, WriteBatchResult
from jiuwenswarm.observability.sink import (
    CommitCallback,
    TrajectoryRecordSink,
    TrajectorySessionSinkRouter,
)
from jiuwenswarm.observability.store import AsyncTrajectoryReader, TrajectoryStore

test_logger = logging.getLogger("tests.session_trajectory_routing")


def _settings(database_root: Path, *, flush_interval_ms: int = 20) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=True,
        database_path=database_root,
        retention_days=7,
        queue_size=64,
        batch_size=8,
        flush_interval_ms=flush_interval_ms,
        poll_interval_ms=2000,
    )


def _record(
    session_id: str,
    trace_id: str,
    span_id: str,
    *,
    revision: int = 1,
) -> SimpleNamespace:
    raw_json = json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {},
                    "scopeSpans": [
                        {
                            "scope": {},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "parentSpanId": "",
                                    "name": "agent.run",
                                    "startTimeUnixNano": "100",
                                    "endTimeUnixNano": "200",
                                    "status": {},
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return SimpleNamespace(
        raw_json=raw_json,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
        session_id=session_id,
        request_id=f"request-{session_id}",
        run_id=f"run-{session_id}",
        agent_mode="agent.work.normal",
        schema_version="1",
        record_revision=revision,
        observed_time_unix_nano=100 + revision,
    )


def _snapshot(
    session_id: str,
    trace_id: str,
    span_id: str,
    revision: int,
) -> SimpleNamespace:
    record = _record(
        session_id,
        trace_id,
        span_id,
        revision=revision,
    )
    record.lifecycle = "running"
    record.update_kind = "stream_chunk"
    return record


@pytest.mark.asyncio
async def test_router_writes_and_reads_each_session_database(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    router = TrajectorySessionSinkRouter(_settings(root))
    first_trace = "1" * 32
    second_trace = "2" * 32

    router.start()
    router.consume(_record("session-a", first_trace, "a" * 16))
    router.consume(_record("session-b", second_trace, "b" * 16))
    assert router.close(timeout=5) is True

    first_path = session_database_path(root, "session-a")
    second_path = session_database_path(root, "session-b")
    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    reader = AsyncTrajectoryReader(root, session_scoped=True)
    first_items, _cursor = await reader.list_traces(
        "session-a",
        limit=10,
        cursor=None,
    )
    second_items, _cursor = await reader.list_traces(
        "session-b",
        limit=10,
        cursor=None,
    )
    assert [item["trace_id"] for item in first_items] == [first_trace]
    assert [item["trace_id"] for item in second_items] == [second_trace]
    assert router.stats().queued == 0
    test_logger.info("router drained two sessions into independently readable files")


@pytest.mark.asyncio
async def test_router_holds_sessionless_child_until_owned_root_arrives(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    router = TrajectorySessionSinkRouter(_settings(root))
    trace_id = "6" * 32
    child = _record("", trace_id, "1" * 16)
    owned_root = _record("session-owner", trace_id, "2" * 16)

    router.start()
    router.consume(child)
    router.consume(owned_root)
    assert router.close(timeout=5) is True

    reader = AsyncTrajectoryReader(root, session_scoped=True)
    detail = await reader.get_trace_records(
        "session-owner",
        trace_id,
        since_revision=0,
        limit=10,
    )
    assert detail is not None
    assert [record["span_id"] for record in detail["records"]] == [
        "1" * 16,
        "2" * 16,
    ]
    test_logger.info("sessionless child retained its order before the owned root")


class _ControlledStore:
    def __init__(self, *, blocked: bool) -> None:
        self.blocked = blocked
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def initialize(self) -> None:
        return

    def delete_expired(self, *, now: int | None = None) -> int:
        return 0

    def write_records(self, records: Sequence[TraceRecordData]) -> WriteBatchResult:
        self.write_started.set()
        if self.blocked:
            self.release_write.wait(timeout=10)
        return WriteBatchResult(
            inserted=len(records),
            conflicts=0,
            updates=(),
        )

    def close(self) -> None:
        return


def test_blocked_session_writer_does_not_delay_another_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    blocked_store = _ControlledStore(blocked=True)
    other_store = _ControlledStore(blocked=False)
    blocked_path = session_database_path(root, "session-blocked")

    def _sink_factory(
        settings: TrajectoryStoreSettings,
        on_commit: CommitCallback | None,
    ) -> TrajectoryRecordSink:
        store = blocked_store if settings.database_path == blocked_path else other_store
        return TrajectoryRecordSink(settings, on_commit=on_commit, store=store)

    router = TrajectorySessionSinkRouter(
        _settings(root),
        sink_factory=_sink_factory,
    )
    router.start()
    try:
        router.consume(
            _record("session-blocked", "3" * 32, "c" * 16),
        )
        assert blocked_store.write_started.wait(timeout=5)
        router.consume(
            _record("session-free", "4" * 32, "d" * 16),
        )
        assert other_store.write_started.wait(timeout=2)
    finally:
        blocked_store.release_write.set()
        assert router.close(timeout=5) is True
    assert router.stats().committed == 2
    test_logger.info("one blocked session did not head-of-line block another writer")


def test_same_session_snapshots_coalesce_and_final_record_wins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    settings = _settings(root, flush_interval_ms=100)
    router = TrajectorySessionSinkRouter(settings)
    trace_id = "5" * 32
    span_id = "e" * 16
    final_record = _record("session-one", trace_id, span_id, revision=51)

    router.start()
    for revision in range(1, 51):
        router.consume_snapshot(
            _snapshot("session-one", trace_id, span_id, revision),
        )
    router.consume(final_record)
    assert router.close(timeout=5) is True

    store = TrajectoryStore(session_database_path(root, "session-one"))
    store.initialize()
    try:
        assert store.fetch_raw(trace_id, span_id) == final_record.raw_json
    finally:
        store.close()
    stats = router.stats()
    assert stats.coalesced > 0
    assert stats.dropped_final == 0
    test_logger.info("same-session snapshot burst coalesced before its ordered final")


def test_session_delete_waits_only_for_target_ingress(tmp_path: Path) -> None:
    router = TrajectorySessionSinkRouter(_settings(tmp_path / "sessions"))

    class _JoinMustNotRunQueue:
        def join(self) -> None:
            raise AssertionError("global ingress join must not be used")

    router._queue = _JoinMustNotRunQueue()
    router._ingress_pending["session-b"] = 1
    router.begin_session_delete("session-a", timeout=0.1)
    test_logger.info("Session deletion did not join the process-wide ingress queue")


def test_idle_retirement_keeps_route_registered_when_join_times_out(
    tmp_path: Path,
) -> None:
    router = TrajectorySessionSinkRouter(_settings(tmp_path / "sessions"))

    class _TimedOutRoute:
        def __init__(self) -> None:
            self.stop_requested = False

        def can_retire(self, now: float) -> bool:
            return True

        def request_stop(self) -> None:
            self.stop_requested = True

        def join(self, timeout: float) -> bool:
            return False

    route = _TimedOutRoute()
    router._routes["session-a"] = route

    router._retire_idle_routes()

    assert route.stop_requested is True
    assert router._routes["session-a"] is route
    test_logger.info("timed-out idle writer stayed registered against duplication")
