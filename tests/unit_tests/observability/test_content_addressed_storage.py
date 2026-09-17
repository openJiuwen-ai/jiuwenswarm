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
        schema_version="2",
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


async def _resolve(database_path: Path, heads: list[str], *, since: int = 0):
    from jiuwenswarm.observability.store import AsyncTrajectoryReader

    reader = AsyncTrajectoryReader(database_path, session_scoped=False)
    return await reader.resolve_sequences("session-1", heads, since_revision=since)


def test_a_chain_resolves_into_its_elements_in_order(tmp_path: Path) -> None:
    import asyncio

    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    sequence = _sequence("gen_ai.input.messages", ["one", "two", "three"])
    try:
        store.write_records([_record("a" * 16, sequence)], ())
    finally:
        store.close()

    resolved = asyncio.run(_resolve(database_path, [sequence.seq_hash]))

    elements = resolved["sequences"][sequence.seq_hash]
    assert [resolved["blobs"][element] for element in elements] == ["one", "two", "three"]


def test_a_resumed_reader_is_not_sent_what_it_already_holds(tmp_path: Path) -> None:
    """The reader's revision decides what crosses the wire, not the page size."""
    import asyncio

    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        early = _sequence("gen_ai.input.messages", ["one", "two"])
        store.write_records([_record("a" * 16, early)], ())
        grown = _sequence("gen_ai.input.messages", ["one", "two", "three"])
        store.write_records([_record("b" * 16, grown)], ())
    finally:
        store.close()

    connection = sqlite3.connect(database_path)
    try:
        first_batch = int(
            connection.execute(
                "SELECT MIN(first_change_seq) FROM trajectory_blobs"
            ).fetchone()[0]
        )
    finally:
        connection.close()

    cold = asyncio.run(_resolve(database_path, [grown.seq_hash]))
    resumed = asyncio.run(_resolve(database_path, [grown.seq_hash], since=first_batch))

    # The chain is stated in full either way; only its content is withheld.
    assert cold["sequences"][grown.seq_hash] == resumed["sequences"][grown.seq_hash]
    assert len(cold["blobs"]) == 3
    assert set(resumed["blobs"]) == {grown.nodes[-1].blob_hash}
    test_logger.info("resumed read carried %d of %d elements", len(resumed["blobs"]), 3)


def test_sequences_shared_by_several_records_are_walked_once(tmp_path: Path) -> None:
    """Spans sharing a prefix resolve together without repeating it."""
    import asyncio

    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    short = _sequence("gen_ai.input.messages", ["one", "two"])
    long = _sequence("gen_ai.input.messages", ["one", "two", "three", "four"])
    try:
        store.write_records([_record("a" * 16, short), _record("b" * 16, long)], ())
    finally:
        store.close()

    resolved = asyncio.run(_resolve(database_path, [short.seq_hash, long.seq_hash]))

    assert resolved["sequences"][short.seq_hash] == resolved["sequences"][long.seq_hash][:2]
    # Four distinct elements back two chains of two and four.
    assert len(resolved["blobs"]) == 4


def test_the_default_export_leaves_no_reference_behind(tmp_path: Path) -> None:
    """An export is read by tools that know nothing of this addressing.

    A reference they cannot resolve is worse than the bytes it saved, so the
    default format states every attribute in full.
    """
    import asyncio
    import base64

    from jiuwenswarm.observability.store import AsyncTrajectoryReader

    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    sequence = _sequence("gen_ai.input.messages", ['{"role":"user"}', '{"role":"assistant"}'])
    try:
        record = _record("a" * 16, sequence)
        store.write_records(
            [
                TraceRecordData(
                    **{
                        **{
                            field: getattr(record, field)
                            for field in record.__dataclass_fields__
                            if field not in {"raw_json", "lifecycle"}
                        },
                        "raw_json": (
                            b'{"resourceSpans":[{"scopeSpans":[{"spans":[{'
                            b'"traceId":"' + _TRACE_ID.encode() + b'","spanId":"'
                            + (b"a" * 16) + b'","name":"chat","attributes":[{'
                            b'"key":"gen_ai.input.messages","value":{"stringValue":"@oj-seq:1:'
                            + sequence.seq_hash.encode() + b':2"}}]}]}]}]}'
                        ),
                        "lifecycle": "final",
                    }
                )
            ],
            (),
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path, session_scoped=False)
    records, _epoch, _revision, resolved = asyncio.run(
        reader.get_session_archive_records("session-1", rehydrate=True)
    )

    assert records
    import json

    raw = base64.b64decode(records[0]["raw_json_base64"]).decode()
    assert "@oj-seq:" not in raw
    attribute = (
        json.loads(raw)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"][0]
    )
    assert json.loads(attribute["value"]["stringValue"]) == [
        {"role": "user"},
        {"role": "assistant"},
    ]
    # Nothing is left for the reader to resolve, so no dictionary is sent.
    assert resolved == {"sequences": {}, "blobs": {}}


def test_the_addressed_export_is_self_contained(tmp_path: Path) -> None:
    """The small format still carries everything needed to read it."""
    import asyncio

    from jiuwenswarm.observability.store import AsyncTrajectoryReader

    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    sequence = _sequence("gen_ai.input.messages", ["one", "two", "three"])
    try:
        store.write_records([_record("b" * 16, sequence)], ())
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path, session_scoped=False)
    records, _epoch, _revision, resolved = asyncio.run(
        reader.get_session_archive_records("session-1", rehydrate=False)
    )

    heads = {
        reference["hash"]
        for record in records
        for reference in (record.get("sequences") or {}).values()
    }
    assert heads <= set(resolved["sequences"])
    for elements in resolved["sequences"].values():
        assert all(element in resolved["blobs"] for element in elements)
    test_logger.info("addressed export carried %d chains", len(resolved["sequences"]))
