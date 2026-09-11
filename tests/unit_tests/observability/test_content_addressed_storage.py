# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Storage of content-addressed sequences behind the trajectory records."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.observability.models import (
    AddressedSequenceData,
    SequenceNodeData,
    TraceRecordData,
)
from jiuwenswarm.observability.store import TrajectoryStore

test_logger = logging.getLogger("tests.content_addressed_storage")

_TRACE_ID = "a" * 32


def _sequence(key: str, elements: list[str]) -> AddressedSequenceData:
    """Build a chain the way Agent Core does: seq(i) = H(seq(i-1) || H(e))."""
    import hashlib

    nodes: list[SequenceNodeData] = []
    blobs: dict[str, bytes] = {}
    previous: str | None = None
    for depth, element in enumerate(elements, start=1):
        content = element.encode()
        blob_hash = hashlib.sha256(content).hexdigest()
        blobs[blob_hash] = content
        digest = hashlib.sha256()
        digest.update((previous or "").encode())
        digest.update(b"\x00")
        digest.update(blob_hash.encode())
        seq_hash = digest.hexdigest()
        nodes.append(SequenceNodeData(seq_hash, previous, blob_hash, depth))
        previous = seq_hash
    return AddressedSequenceData(
        key=key,
        seq_hash=nodes[-1].seq_hash,
        depth=len(nodes),
        nodes=tuple(nodes),
        blobs=blobs,
    )


def _record(span_id: str, sequence: AddressedSequenceData, *, created_at: int = 1_700_000) -> TraceRecordData:
    return TraceRecordData(
        raw_json=b'{"resourceSpans":[]}',
        raw_sha256="0" * 64,
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        start_time_unix_nano=1,
        end_time_unix_nano=2,
        session_id="session-1",
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
        source="processor",
        created_at=created_at,
        execution_subject_id="main",
        lifecycle="running",
        record_revision=1,
        observed_time_unix_nano=1,
        update_kind="attributes",
        logical_size_bytes=4096,
        sequences=(sequence,),
    )


def _counts(database_path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(database_path)
    try:
        blobs = connection.execute("SELECT COUNT(*) FROM trajectory_blobs").fetchone()[0]
        nodes = connection.execute("SELECT COUNT(*) FROM trajectory_sequences").fetchone()[0]
        return int(blobs), int(nodes)
    finally:
        connection.close()


def test_a_growing_conversation_stores_only_what_it_added(tmp_path: Path) -> None:
    """Restating a conversation costs its increment, not its length."""
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        first = _sequence("gen_ai.input.messages", [f"m{i}" for i in range(5)])
        store.write_records([_record("b" * 16, first)], ())
        after_first = _counts(database_path)

        grown = _sequence("gen_ai.input.messages", [f"m{i}" for i in range(7)])
        store.write_records([_record("c" * 16, grown)], ())
        after_second = _counts(database_path)
    finally:
        store.close()

    assert after_first == (5, 5)
    # Two more messages, two more blobs, two more nodes -- the shared prefix
    # of five is stored once.
    assert after_second == (7, 7)
    test_logger.info("growth cost %s new rows", tuple(b - a for a, b in zip(after_first, after_second)))


def test_restating_the_same_content_writes_nothing_new(tmp_path: Path) -> None:
    """A tool definition restated on every call is stored once."""
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    tools = _sequence("gen_ai.tool.definitions", ["tool-a", "tool-b", "tool-c"])
    try:
        for index in range(6):
            store.write_records([_record(f"{index:016x}", tools)], ())
        counts = _counts(database_path)
    finally:
        store.close()

    assert counts == (3, 3)


def test_a_reference_is_stored_instead_of_the_rebuilt_size(tmp_path: Path) -> None:
    """The budget column states what a reader receives, not what is stored."""
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [_record("d" * 16, _sequence("gen_ai.input.messages", ["one", "two"]))],
            (),
        )
    finally:
        store.close()

    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT raw_size_bytes, LENGTH(raw_json) AS stored FROM trajectory_current_records"
        ).fetchone()
    finally:
        connection.close()

    assert int(row[0]) == 4096
    assert int(row[1]) < int(row[0])


def test_retention_keeps_content_that_is_still_being_restated(tmp_path: Path) -> None:
    """Content ages by when it was last referenced, not when it first appeared.

    A tool definition stated at the start of a long session is referenced
    again on every call; expiring it by first appearance would break records
    written minutes ago.
    """
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    tools = _sequence("gen_ai.tool.definitions", ["tool-a"])
    day = 86_400
    try:
        store.write_records([_record("e" * 16, tools, created_at=day)], ())
        # The same content restated much later refreshes when it was needed.
        store.write_records([_record("f" * 16, tools, created_at=day * 10)], ())
        store.delete_expired(now=day * 10 + 60)
        counts = _counts(database_path)
    finally:
        store.close()

    assert counts == (1, 1)
