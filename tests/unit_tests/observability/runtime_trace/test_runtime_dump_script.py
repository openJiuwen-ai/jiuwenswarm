# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the read-only dump script."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from jiuwenswarm.runtime_trace.consumer import _Pending
from jiuwenswarm.runtime_trace.store import RuntimeTraceStore


class _Record:
    def __init__(
        self,
        *,
        trace_id: str = "a" * 32,
        span_id: str = "b" * 16,
        parent_span_id: str | None = None,
        start_ns: int = 1,
        end_ns: int = 2,
    ) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.start_time_unix_nano = start_ns
        self.end_time_unix_nano = end_ns
        self.record_revision = 1
        self.observed_time_unix_nano = 3
        self.raw_json = b'{"resourceSpans": []}'
        self.sequences = ()


def _database(
    tmp_path: Path,
    *,
    run_id: str = "c" * 32,
    role: str = "worker",
    records: list[tuple[_Record, str, str | None]] | None = None,
) -> Path:
    store = RuntimeTraceStore(
        root=tmp_path / "runtime",
        role=role,
        run_id=run_id,
        pid=1,
        started_at_ns=time.time_ns(),
        retention_days=7,
    )
    store.open()
    store.write_batch(
        [
            _Pending(record, name, outcome)
            for record, name, outcome in (
                records or [(_Record(), "startup", "success")]
            )
        ]
    )
    store.close(time.time_ns())
    return store.path


def test_dump_script_reads_closed_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "dump_runtime_trace.py"), str(database)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "startup" in result.stdout
    assert "closed=yes" in result.stdout
    assert "role=worker" in result.stdout


def test_dump_script_merges_front_and_worker_databases(tmp_path: Path) -> None:
    trace_id = "d" * 32
    spawn = _Record(
        trace_id=trace_id,
        span_id="1" * 16,
        start_ns=1_000,
        end_ns=2_000,
    )
    startup = _Record(
        trace_id=trace_id,
        span_id="2" * 16,
        parent_span_id="1" * 16,
        start_ns=3_000,
        end_ns=9_000,
    )
    front = _database(
        tmp_path,
        run_id="e" * 32,
        role="front",
        records=[(spawn, "worker.spawn", "success")],
    )
    worker = _database(
        tmp_path,
        run_id="f" * 32,
        role="worker",
        records=[(startup, "startup", "success")],
    )
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "dump_runtime_trace.py"),
            str(front),
            str(worker),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert f"trace {trace_id}" in result.stdout
    assert "spans=2" in result.stdout
    assert "worker.spawn" in result.stdout
    # The Worker startup is rendered below spawn, i.e. with a tree prefix.
    assert any(
        "`-- startup" in line for line in result.stdout.splitlines()
    )


def test_dump_script_copy_mode_does_not_touch_source(tmp_path: Path) -> None:
    database = _database(tmp_path)
    root = Path(__file__).resolve().parents[4]
    before = database.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "dump_runtime_trace.py"),
            str(database),
            "--copy",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert database.read_bytes() == before
