# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Session-scoped trajectory deletion lifecycle."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.observability.config import (
    TrajectoryStoreSettings,
    session_database_path,
)
from jiuwenswarm.observability.session_delete import (
    TrajectorySessionDeleteLifecycle,
)
from jiuwenswarm.observability.sink import TrajectorySessionSinkRouter

test_logger = logging.getLogger("tests.trajectory_session_delete")

_TRACE_ID = "9" * 32
_SPAN_ID = "d" * 16


class _RecordingBackend:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.abort_error: Exception | None = None

    def begin_session_delete(self, session_id: str) -> None:
        self.events.append(("begin", session_id))

    def abort_session_delete(self, session_id: str) -> None:
        self.events.append(("abort", session_id))
        if self.abort_error is not None:
            raise self.abort_error

    def commit_session_delete(self, session_id: str) -> None:
        self.events.append(("commit", session_id))


def _settings(database_root: Path) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=True,
        database_path=database_root,
        retention_days=7,
        queue_size=32,
        batch_size=8,
        flush_interval_ms=10,
        poll_interval_ms=2000,
    )


def _record(session_id: str) -> SimpleNamespace:
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
                                    "spanId": _SPAN_ID,
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
        span_id=_SPAN_ID,
        parent_span_id=None,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
        session_id=session_id,
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
    )


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not satisfied before timeout")


def test_successful_delete_closes_route_and_removes_sqlite_sidecars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = TrajectorySessionDeleteLifecycle()
    router = TrajectorySessionSinkRouter(_settings(tmp_path / "sessions"))
    lifecycle.set_backend(router)
    monkeypatch.setattr(
        "jiuwenswarm.observability.sink.trajectory_session_accepts_records",
        lifecycle.accepts_records,
    )
    session_id = "session-delete-success"
    database_path = session_database_path(router.settings.database_path, session_id)
    router.start()
    try:
        router.consume(_record(session_id))
        _wait_until(lambda: database_path.is_file() and router.stats().committed == 1)
        lifecycle.begin(session_id)
        for suffix in ("-wal", "-shm"):
            database_path.with_name(f"{database_path.name}{suffix}").touch()
        lifecycle.commit(session_id)
        assert not database_path.exists()
        assert not database_path.with_name(f"{database_path.name}-wal").exists()
        assert not database_path.with_name(f"{database_path.name}-shm").exists()
        assert lifecycle.accepts_records(session_id) is False
    finally:
        assert router.close(timeout=5) is True
    test_logger.info("Session deletion removed the database and both SQLite sidecars")


def test_failed_product_delete_aborts_and_allows_writes_again() -> None:
    lifecycle = TrajectorySessionDeleteLifecycle()
    backend = _RecordingBackend()
    lifecycle.set_backend(backend)

    lifecycle.begin("session-delete-abort")
    assert lifecycle.accepts_records("session-delete-abort") is False
    lifecycle.abort("session-delete-abort")

    assert lifecycle.accepts_records("session-delete-abort") is True
    assert backend.events == [
        ("begin", "session-delete-abort"),
        ("abort", "session-delete-abort"),
    ]


def test_abort_releases_tombstone_even_when_backend_rollback_fails() -> None:
    lifecycle = TrajectorySessionDeleteLifecycle()
    backend = _RecordingBackend()
    backend.abort_error = RuntimeError("rollback failed")
    lifecycle.set_backend(backend)
    lifecycle.begin("session-delete-abort-error")

    with pytest.raises(RuntimeError, match="rollback failed"):
        lifecycle.abort("session-delete-abort-error")

    assert lifecycle.accepts_records("session-delete-abort-error") is True


def test_late_record_is_rejected_while_session_is_tombstoned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lifecycle = TrajectorySessionDeleteLifecycle()
    router = TrajectorySessionSinkRouter(_settings(tmp_path / "sessions"))
    lifecycle.set_backend(router)
    monkeypatch.setattr(
        "jiuwenswarm.observability.sink.trajectory_session_accepts_records",
        lifecycle.accepts_records,
    )
    session_id = "session-delete-late"
    database_path = session_database_path(router.settings.database_path, session_id)
    router.start()
    try:
        lifecycle.begin(session_id)
        router.consume(_record(session_id))
        assert router.stats().dropped == 1
        assert not database_path.exists()
        lifecycle.commit(session_id)
        router.consume(_record(session_id))
        assert router.stats().dropped == 2
        assert not database_path.exists()
    finally:
        assert router.close(timeout=5) is True


def test_repeated_begin_commit_and_abort_are_idempotent() -> None:
    lifecycle = TrajectorySessionDeleteLifecycle()
    backend = _RecordingBackend()
    lifecycle.set_backend(backend)

    lifecycle.begin("session-delete-repeat")
    lifecycle.begin("session-delete-repeat")
    lifecycle.commit("session-delete-repeat")
    lifecycle.commit("session-delete-repeat")
    lifecycle.abort("session-delete-repeat")

    assert backend.events == [
        ("begin", "session-delete-repeat"),
        ("commit", "session-delete-repeat"),
    ]
    assert lifecycle.accepts_records("session-delete-repeat") is False
