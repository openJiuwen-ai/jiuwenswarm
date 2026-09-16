# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the bounded trajectory consumer and writer lifecycle."""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.observability.config import TrajectoryStoreSettings
from jiuwenswarm.observability.models import TraceRecordData, WriteBatchResult
from jiuwenswarm.observability.sink import TrajectoryRecordSink
from jiuwenswarm.observability.store import TrajectoryStore

test_logger = logging.getLogger("tests.trajectory_sink")

_TRACE_ID = "3" * 32
_SPAN_ID = "c" * 16


def _settings(
    database_path: Path,
    *,
    queue_size: int = 16,
    batch_size: int = 8,
    flush_interval_ms: int = 10,
) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=True,
        database_path=database_path,
        retention_days=7,
        queue_size=queue_size,
        batch_size=batch_size,
        flush_interval_ms=flush_interval_ms,
        poll_interval_ms=2000,
    )


def _record(*, span_id: str = _SPAN_ID) -> SimpleNamespace:
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
                                    "traceId": _TRACE_ID,
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
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
        session_id="session-1",
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
    )


def _snapshot(revision: int) -> SimpleNamespace:
    record = _record()
    record.record_revision = revision
    record.observed_time_unix_nano = 100 + revision
    record.update_kind = "stream_chunk"
    record.lifecycle = "running"
    return record


class _BlockingStore:
    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def initialize(self) -> None:
        return

    def delete_expired(self, *, now: int | None = None) -> int:
        return 0

    def write_records(self, records: Sequence[TraceRecordData]) -> WriteBatchResult:
        self.write_started.set()
        self.release_write.wait(timeout=10)
        return WriteBatchResult(
            inserted=len(records),
            conflicts=0,
            updates=(),
        )

    def close(self) -> None:
        return


class _BusyThenSuccessfulStore(_BlockingStore):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def write_records(self, records: Sequence[TraceRecordData]) -> WriteBatchResult:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return WriteBatchResult(inserted=len(records), conflicts=0, updates=())


class _RecordingStore(_BlockingStore):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[tuple[TraceRecordData, ...]] = []

    def write_records(self, records: Sequence[TraceRecordData]) -> WriteBatchResult:
        self.batches.append(tuple(records))
        self.write_started.set()
        return WriteBatchResult(inserted=len(records), conflicts=0, updates=())


class _PausingInitializeStore(_BlockingStore):
    def __init__(self) -> None:
        super().__init__()
        self.initialize_entered = threading.Event()
        self.release_initialize = threading.Event()

    def initialize(self) -> None:
        self.initialize_entered.set()
        self.release_initialize.wait(timeout=10)


class _PausingQueue(queue.Queue):
    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self.put_entered = threading.Event()
        self.release_put = threading.Event()

    def put_nowait(self, item) -> None:
        self.put_entered.set()
        self.release_put.wait(timeout=10)
        super().put_nowait(item)


def test_sink_commits_before_notifying_and_drains_on_close(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    updates = []
    sink = TrajectoryRecordSink(
        _settings(database_path),
        on_commit=lambda committed: updates.extend(committed),
    )
    record = _record()

    sink.start()
    sink.consume(record)
    assert sink.close(timeout=5) is True

    stats = sink.stats()
    verifier = TrajectoryStore(database_path)
    verifier.initialize()
    try:
        stored_raw = verifier.fetch_raw(_TRACE_ID, _SPAN_ID)
    finally:
        verifier.close()

    assert stats.accepted == 1
    assert stats.committed == 1
    assert stats.dropped == 0
    assert stats.failed == 0
    assert stored_raw == record.raw_json
    assert len(updates) == 1
    assert updates[0].session_id == "session-1"
    assert updates[0].trace_id == _TRACE_ID
    assert updates[0].revision >= 1
    test_logger.info("sink drained one record and emitted committed revision")


def test_sink_drops_without_blocking_when_not_started(tmp_path: Path) -> None:
    sink = TrajectoryRecordSink(_settings(tmp_path / "trajectory.sqlite3"))

    sink.consume(_record())

    stats = sink.stats()
    assert stats.accepted == 0
    assert stats.dropped == 1
    assert stats.queued == 0
    test_logger.info("inactive sink rejected record without queueing")


def test_sink_rejects_subject_session_owned_by_another_chat(tmp_path: Path) -> None:
    sink = TrajectoryRecordSink(_settings(tmp_path / "trajectory.sqlite3"))
    record = _record()
    record.execution_subject_session_id = "session-previous_sub_general_deadbeef"

    sink.start()
    try:
        sink.consume(record)
        stats = sink.stats()
        assert stats.accepted == 0
        assert stats.failed == 1
        assert stats.queued == 0
    finally:
        assert sink.close(timeout=5) is True


def test_sink_queue_full_drops_without_blocking(tmp_path: Path) -> None:
    store = _BlockingStore()
    sink = TrajectoryRecordSink(
        _settings(
            tmp_path / "trajectory.sqlite3",
            queue_size=1,
            batch_size=1,
        ),
        store=store,
    )
    sink.start()
    try:
        sink.consume(_record(span_id="1" * 16))
        assert store.write_started.wait(timeout=5)
        sink.consume(_record(span_id="2" * 16))

        started_at = time.monotonic()
        sink.consume(_record(span_id="3" * 16))
        elapsed = time.monotonic() - started_at

        stats = sink.stats()
        assert elapsed < 1.0
        assert stats.accepted == 2
        assert stats.dropped == 1
        assert stats.queued == 1
    finally:
        store.release_write.set()
        assert sink.close(timeout=5) is True
    test_logger.info("full sink queue dropped immediately without blocking the caller")


def test_sink_coalesces_pending_live_snapshots_by_identity(tmp_path: Path) -> None:
    store = _BlockingStore()
    sink = TrajectoryRecordSink(
        _settings(tmp_path / "trajectory.sqlite3", queue_size=16, batch_size=1),
        store=store,
    )
    sink.start()
    try:
        sink.consume_snapshot(_snapshot(1))
        assert store.write_started.wait(timeout=5)
        for revision in range(2, 11):
            sink.consume_snapshot(_snapshot(revision))
        stats_while_blocked = sink.stats()
        assert stats_while_blocked.queued == 1
        assert stats_while_blocked.coalesced >= 8
    finally:
        store.release_write.set()
        assert sink.close(timeout=5) is True
    assert sink.stats().dropped_final == 0
    test_logger.info("live snapshot flood retained only the newest pending identity")


def test_sink_debounces_snapshot_flood_before_sqlite_write(tmp_path: Path) -> None:
    store = _RecordingStore()
    sink = TrajectoryRecordSink(
        _settings(
            tmp_path / "trajectory.sqlite3",
            batch_size=8,
            flush_interval_ms=100,
        ),
        store=store,
    )
    sink.start()
    try:
        for revision in range(1, 51):
            sink.consume_snapshot(_snapshot(revision))
        assert store.write_started.wait(timeout=2)
    finally:
        assert sink.close(timeout=5) is True

    assert len(store.batches) == 1
    assert len(store.batches[0]) == 1
    assert store.batches[0][0].record_revision == 50
    assert sink.stats().coalesced == 49
    test_logger.info("snapshot debounce persisted one latest revision from a burst")


def test_sink_rejects_invalid_raw_type_without_raising(tmp_path: Path) -> None:
    sink = TrajectoryRecordSink(_settings(tmp_path / "trajectory.sqlite3"))
    record = _record()
    record.raw_json = "not-bytes"

    sink.consume(record)

    stats = sink.stats()
    assert stats.failed == 1
    assert stats.dropped == 0
    test_logger.info("invalid Core record isolated from the caller")


def test_sink_close_waits_for_an_accepted_inflight_enqueue(tmp_path: Path) -> None:
    sink = TrajectoryRecordSink(_settings(tmp_path / "trajectory.sqlite3"))
    pausing_queue = _PausingQueue(maxsize=16)
    sink._queue = pausing_queue
    sink.start()

    producer = threading.Thread(target=sink.consume, args=(_record(),))
    producer.start()
    assert pausing_queue.put_entered.wait(timeout=5)

    close_result: list[bool] = []
    closer = threading.Thread(
        target=lambda: close_result.append(sink.close(timeout=5)),
    )
    closer.start()
    try:
        assert closer.is_alive()
    finally:
        pausing_queue.release_put.set()
    producer.join(timeout=5)
    closer.join(timeout=5)

    stats = sink.stats()
    assert producer.is_alive() is False
    assert closer.is_alive() is False
    assert close_result == [True]
    assert stats.accepted == 1
    assert stats.committed == 1
    assert stats.queued == 0
    test_logger.info("close could not overtake an accepted in-flight enqueue")


def test_sink_retries_transient_sqlite_contention(tmp_path: Path) -> None:
    store = _BusyThenSuccessfulStore(failures=1)
    sink = TrajectoryRecordSink(
        _settings(tmp_path / "trajectory.sqlite3"),
        store=store,
    )
    sink.start()
    sink.consume(_record())

    assert sink.close(timeout=5) is True

    stats = sink.stats()
    assert store.attempts == 2
    assert stats.accepted == 1
    assert stats.committed == 1
    assert stats.failed == 0
    test_logger.info("transient SQLite lock retried within the bounded drain")


def test_real_sqlite_busy_retry_finishes_before_default_close_deadline(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    initializer = TrajectoryStore(database_path)
    initializer.initialize()
    initializer.close()

    sink = TrajectoryRecordSink(_settings(database_path))
    sink.start()
    locker = sqlite3.connect(str(database_path), check_same_thread=False)
    locker.execute("PRAGMA busy_timeout=1000")
    locker.execute("BEGIN IMMEDIATE")
    released = threading.Event()

    def _release_lock() -> None:
        released.wait(timeout=5.2)
        locker.commit()

    releaser = threading.Thread(target=_release_lock)
    releaser.start()
    started_at = time.monotonic()
    try:
        sink.consume(_record())
        assert sink.close() is True
    finally:
        released.set()
        releaser.join(timeout=10)
        locker.close()
    elapsed = time.monotonic() - started_at

    stats = sink.stats()
    assert elapsed < 15.0
    assert releaser.is_alive() is False
    assert stats.accepted == 1
    assert stats.committed == 1
    assert stats.failed == 0
    test_logger.info("real SQLite lock contention drained before the default deadline")


def test_sink_close_during_startup_cannot_reopen_acceptance(tmp_path: Path) -> None:
    store = _PausingInitializeStore()
    sink = TrajectoryRecordSink(
        _settings(tmp_path / "trajectory.sqlite3"),
        store=store,
    )
    start_errors: list[BaseException] = []
    close_results: list[bool] = []

    def _start() -> None:
        try:
            sink.start()
        except BaseException as exc:
            start_errors.append(exc)

    starter = threading.Thread(target=_start)
    starter.start()
    assert store.initialize_entered.wait(timeout=5)
    closer = threading.Thread(target=lambda: close_results.append(sink.close(timeout=5)))
    closer.start()
    assert sink._stop_requested.wait(timeout=5)
    store.release_initialize.set()
    starter.join(timeout=5)
    closer.join(timeout=5)

    sink.consume(_record())
    stats = sink.stats()
    assert starter.is_alive() is False
    assert closer.is_alive() is False
    assert close_results == [True]
    assert len(start_errors) == 1
    assert isinstance(start_errors[0], RuntimeError)
    assert stats.accepted == 0
    assert stats.dropped == 1
    assert stats.queued == 0
    test_logger.info("startup could not reopen a sink after concurrent close")
