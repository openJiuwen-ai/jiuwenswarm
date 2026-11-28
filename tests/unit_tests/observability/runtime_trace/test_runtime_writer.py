# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for level-aware writer queues."""

from __future__ import annotations

import queue
import time
from pathlib import Path

from jiuwenswarm.runtime_trace.consumer import _Pending
from jiuwenswarm.runtime_trace.writer import RuntimeTraceWriter


class _FakeStore:
    role = "worker"
    run_id = "run"
    root = Path(".")

    def __init__(self) -> None:
        self.batches = []
        self.closed = False
        self.meta = []
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def write_batch(self, items):
        self.batches.append(list(items))
        return {"inserted": len(items), "conflicts": 0, "dropped": 0, "missing": 0}

    def update_worker_meta(self, instance, generation) -> None:
        self.meta.append((instance, generation))

    def close(self, closed_at_ns) -> None:
        self.closed = True

    def sweep(self, cutoff_ns, *, batch_traces):
        return 0


def _item(name: str) -> _Pending:
    return _Pending(record=object(), name=name, outcome="success")


def _writer() -> tuple[RuntimeTraceWriter, _FakeStore]:
    store = _FakeStore()
    return RuntimeTraceWriter(store, retention_days=7), store


def test_root_channel_is_never_evicted() -> None:
    writer, _store = _writer()
    root = _item("startup")
    writer.offer(root, level="root")
    for _ in range(200):
        writer.offer(_item("startup.phase.x"), level="normal")
    # normal fills to 1024 first; force terminal pressure too.
    for _ in range(200):
        writer.offer(_item("worker.exited"), level="terminal")
    assert writer._critical_roots == [root]


def test_terminal_eviction_prefers_failed() -> None:
    writer, _store = _writer()
    for _ in range(128):
        writer.offer(_item("worker.exited"), level="terminal")
    writer.offer(_item("worker.failed"), level="terminal")
    names = [item.name for item in writer._critical_terminal]
    # The failed entry was added after the channel was full, so the oldest
    # exited entry was evicted instead: exited entries are only evicted when
    # nothing earlier in the eviction order exists.
    assert names.count("worker.failed") == 1
    assert names.count("worker.exited") == 127


def test_terminal_evicts_failed_before_exited() -> None:
    writer, _store = _writer()
    writer.offer(_item("worker.failed"), level="terminal")
    for _ in range(127):
        writer.offer(_item("worker.exited"), level="terminal")
    writer.offer(_item("worker.ipc_lost"), level="terminal")
    names = [item.name for item in writer._critical_terminal]
    assert "worker.failed" not in names
    assert names.count("worker.exited") == 127
    assert "worker.ipc_lost" in names


def test_event_and_terminal_are_independent() -> None:
    writer, _store = _writer()
    for _ in range(128):
        writer.offer(_item("worker.exited"), level="terminal")
    for _ in range(128):
        writer.offer(_item("worker.spawn"), level="event")
    assert len(writer._critical_terminal) == 128
    assert len(writer._critical_events) == 128
    writer.offer(_item("worker.restart"), level="event")
    assert len(writer._critical_terminal) == 128


def test_normal_channel_drops_new_records() -> None:
    writer, _store = _writer()
    for _ in range(1024):
        writer.offer(_item("startup.phase.x"), level="normal")
    writer.offer(_item("startup.phase.y"), level="normal")
    assert writer._normal.qsize() == 1024
    assert writer.stats()["dropped"] == 1


def test_offer_after_request_stop_is_rejected() -> None:
    writer, _store = _writer()
    writer.request_stop()
    writer.offer(_item("worker.exited"), level="terminal")
    assert writer.stats()["rejected"] == 1
    assert writer._critical_terminal == []


def test_drain_batch_rotates_levels() -> None:
    writer, _store = _writer()
    writer.offer(_item("startup"), level="root")
    for _ in range(100):
        writer.offer(_item("worker.exited"), level="terminal")
        writer.offer(_item("worker.spawn"), level="event")
        writer.offer(_item("startup.phase.x"), level="normal")
    batch = writer._drain_batch()
    names = [item.name for item in batch]
    assert names[0] == "startup"
    assert names.count("worker.exited") == 32
    assert names.count("worker.spawn") == 32
    assert names.count("startup.phase.x") == 64


def test_writer_drains_and_closes_store() -> None:
    writer, store = _writer()
    writer.set_worker_meta("abc", 2)
    writer.start()
    try:
        for _ in range(150):
            writer.offer(_item("startup.phase.x"), level="normal")
        writer.offer(_item("startup"), level="root")
        writer.offer(_item("worker.exited"), level="terminal")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and writer.stats()["committed"] < 152:
            time.sleep(0.01)
        writer.request_stop()
        writer.join(timeout=5)
    finally:
        writer.join(timeout=5)
    assert not writer.is_alive()
    assert store.opened and store.closed
    assert sum(len(batch) for batch in store.batches) == 152
    assert ("abc", 2) in store.meta


def test_writer_degrades_when_store_build_fails() -> None:
    store = _FakeStore()

    def broken_open() -> None:
        raise OSError("cannot create database")

    store.open = broken_open  # type: ignore[method-assign]
    writer = RuntimeTraceWriter(store, retention_days=7)
    writer.start()
    writer.join(timeout=5)
    assert writer.stats()["build_failed"] == 1
    writer.offer(_item("worker.exited"), level="terminal")
    assert writer.stats()["rejected"] == 1
