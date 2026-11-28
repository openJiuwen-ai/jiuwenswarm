# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the run-scoped SQLite store."""

from __future__ import annotations

import sqlite3
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from jiuwenswarm.runtime_trace.consumer import _Pending
from jiuwenswarm.runtime_trace.store import (
    RuntimeTraceStore,
    reclaim_history,
    sqlite_files,
)


@dataclass
class _Record:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_time_unix_nano: int = 1
    end_time_unix_nano: int = 2
    record_revision: int = 1
    observed_time_unix_nano: int = 3
    raw_json: bytes = b'{"resourceSpans": []}'
    sequence_tuple: tuple = ()

    @property
    def sequences(self) -> tuple:
        return self.sequence_tuple


def _store(tmp_path: Path, run_id: str = "a" * 32) -> RuntimeTraceStore:
    return RuntimeTraceStore(
        root=tmp_path / "runtime",
        role="worker",
        run_id=run_id,
        pid=1234,
        started_at_ns=time.time_ns(),
        retention_days=7,
    )


def _pending(name: str = "startup", outcome: str | None = "success") -> _Pending:
    return _Pending(record=_Record(trace_id="t" * 32, span_id="s" * 16), name=name, outcome=outcome)


def test_open_creates_schema_and_meta_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    try:
        conn = sqlite3.connect(store.path)
        try:
            meta = conn.execute(
                "SELECT run_id, role, pid, closed_at, schema_version FROM meta"
            ).fetchone()
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
    finally:
        store.close(time.time_ns())
    assert meta == ("a" * 32, "worker", 1234, None, 1)
    assert {"meta", "span_records", "conflicts"} <= tables
    assert store.lease_path.exists()
    assert store.path.exists()


def test_write_batch_persists_name_and_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    try:
        result = store.write_batch([_pending("worker.exited", "success")])
        assert result["inserted"] == 1
        conn = sqlite3.connect(store.path)
        try:
            row = conn.execute(
                "SELECT name, outcome, parent_span_id, raw_json FROM span_records"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "worker.exited"
        assert row[1] == "success"
        assert zlib.decompress(row[3])
    finally:
        store.close(time.time_ns())


def test_duplicate_write_keeps_first_and_records_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    try:
        item = _pending("startup")
        store.write_batch([item])
        result = store.write_batch([item])
    finally:
        store.close(time.time_ns())
    assert result["inserted"] == 0
    assert result["conflicts"] == 1
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM span_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0] == 1
    finally:
        conn.close()


def test_records_with_sequences_are_dropped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    try:
        pending = _Pending(
            record=_Record(
                trace_id="t" * 32,
                span_id="s" * 16,
                sequence_tuple=(object(),),
            ),
            name="task.x",
            outcome="success",
        )
        result = store.write_batch([pending])
    finally:
        store.close(time.time_ns())
    assert result["dropped"] == 1
    assert result["inserted"] == 0


def test_missing_meta_is_counted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    try:
        result = store.write_batch([_pending(name="", outcome=None)])
    finally:
        store.close(time.time_ns())
    assert result["missing"] == 1


def test_worker_meta_and_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    store.update_worker_meta("instance", 4)
    store.close(time.time_ns())
    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute(
            "SELECT worker_instance, generation, closed_at, last_write_at FROM meta"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "instance"
    assert row[1] == 4
    assert row[2] is not None
    assert row[3] is not None


def test_sweep_removes_expired_trace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open()
    try:
        expired = _Pending(
            record=_Record(
                trace_id="e" * 32,
                span_id="1" * 16,
                start_time_unix_nano=1,
                end_time_unix_nano=2,
            ),
            name="startup",
            outcome="success",
        )
        fresh = _Pending(
            record=_Record(
                trace_id="f" * 32,
                span_id="2" * 16,
                start_time_unix_nano=time.time_ns(),
                end_time_unix_nano=time.time_ns() + 10**9,
            ),
            name="startup",
            outcome="success",
        )
        store.write_batch([expired, fresh])
        store.sweep(time.time_ns() - 10**9, batch_traces=10)
        conn = sqlite3.connect(store.path)
        try:
            traces = {
                row[0]
                for row in conn.execute("SELECT DISTINCT trace_id FROM span_records")
            }
        finally:
            conn.close()
        assert traces == {"f" * 32}
    finally:
        store.close(time.time_ns())


def test_reclaim_history_deletes_expired_closed_database(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _store(tmp_path)
    store.open()
    store.close(time.time_ns() - 30 * 86400 * 10**9)
    main, wal, shm = sqlite_files(store.path)
    assert main.exists()

    removed = reclaim_history(root, "worker", 7, exclude_run_id="other")
    assert removed == 1
    assert not main.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert store.lease_path.exists()


def test_reclaim_history_skips_live_database(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _store(tmp_path)
    store.open()
    try:
        removed = reclaim_history(root, "worker", 7, exclude_run_id="other")
        assert removed == 0
        assert store.path.exists()
    finally:
        store.close(time.time_ns())


def test_reclaim_history_skips_fresh_closed_database(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _store(tmp_path)
    store.open()
    store.close(time.time_ns())
    removed = reclaim_history(root, "worker", 7, exclude_run_id="other")
    assert removed == 0
    assert store.path.exists()
