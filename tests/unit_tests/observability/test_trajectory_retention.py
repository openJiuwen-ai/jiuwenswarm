# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for page-wise trajectory retention and its checkpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.observability.models import TraceRecordData
from jiuwenswarm.observability.otlp_payload import js_bigint
from jiuwenswarm.observability.retention import checkpoint_sequence_heads, record_view_facts
from jiuwenswarm.observability.store import AsyncTrajectoryReader, TrajectoryStore
from tests.unit_tests.observability.retention_fixture_builder import (
    FIXTURE_DIRECTORY,
    QUIRKS_SESSION_ID,
    SINGLE_SESSION_ID,
    build_fixtures,
    quirk_records,
    retention_now,
    single_agent_records,
)

test_logger = logging.getLogger("tests.trajectory_retention")

_SESSION_ID = "session-retention"
_DAY_SECONDS = 86_400


def _hex(label: str, width: int) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:width]


def _record(
    *,
    trace: str,
    span: str,
    created_at: int,
    turn: str | None = None,
    number: int | None = None,
    parent: str | None = None,
    subject: tuple[str, ...] | None = None,
    mode: str = "agent.work.normal",
    start: int = 100,
    running: bool = False,
) -> TraceRecordData:
    """Build one span record stating the turn and subject it belongs to.

    ``subject`` is the subject id, display name, kind and, optionally, parent id.
    """
    attributes: list[dict[str, Any]] = [
        {"key": "gen_ai.conversation.id", "value": {"stringValue": _SESSION_ID}},
    ]
    if turn is not None:
        attributes.append({"key": "openjiuwen.turn.id", "value": {"stringValue": turn}})
    if number is not None:
        attributes.append({"key": "openjiuwen.turn.number", "value": {"intValue": str(number)}})
    if subject is not None:
        for key, value in zip(("id", "display_name", "kind", "parent_id"), subject):
            attributes.append({"key": f"openjiuwen.execution.subject.{key}", "value": {"stringValue": value}})
    span_body: dict[str, Any] = {
        "traceId": _hex(trace, 32),
        "spanId": _hex(span, 16),
        "parentSpanId": "" if parent is None else _hex(parent, 16),
        "name": span,
        "startTimeUnixNano": str(start),
        "status": {},
        "attributes": attributes,
    }
    if not running:
        span_body["endTimeUnixNano"] = str(start + 10)
    raw_json = json.dumps(
        {"resourceSpans": [{"resource": {}, "scopeSpans": [{"scope": {}, "spans": [span_body]}]}]},
        separators=(",", ":"),
    ).encode("utf-8")
    core = SimpleNamespace(
        raw_json=raw_json,
        trace_id=_hex(trace, 32),
        span_id=_hex(span, 16),
        parent_span_id=None if parent is None else _hex(parent, 16),
        start_time_unix_nano=start,
        end_time_unix_nano=start + 10,
        observed_time_unix_nano=start + 10,
        record_revision=1,
        update_kind="started",
        lifecycle="running",
        session_id=_SESSION_ID,
        request_id="request",
        run_id="run",
        agent_mode=mode,
        schema_version="2",
        execution_subject_id=None if subject is None else subject[0],
    )
    if running:
        return TraceRecordData.from_core_snapshot(core, created_at=created_at)
    return TraceRecordData.from_core_record(core, created_at=created_at)


def _write(database_path: Path, records: list[TraceRecordData]) -> None:
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        store.write_records(records)
    finally:
        store.close()


def _retire(database_path: Path, now: int) -> int:
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        return store.delete_expired(now=now)
    finally:
        store.close()


def _span_names(database_path: Path) -> list[str]:
    connection = sqlite3.connect(database_path)
    try:
        names = []
        for (raw_json,) in connection.execute(
            """
            SELECT COALESCE(NULLIF(current.raw_json, X''), archive.raw_json)
            FROM trajectory_current_records AS current
            LEFT JOIN otlp_span_records AS archive
                ON archive.trace_id = current.trace_id AND archive.span_id = current.span_id
            ORDER BY current.change_seq
            """
        ):
            payload = json.loads(zlib.decompress(raw_json))
            names.append(payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"])
        return names
    finally:
        connection.close()


def _raw_payloads(database_path: Path) -> list[bytes]:
    connection = sqlite3.connect(database_path)
    try:
        return [
            zlib.decompress(raw_json)
            for (raw_json,) in connection.execute(
                """
                SELECT COALESCE(NULLIF(current.raw_json, X''), archive.raw_json)
                FROM trajectory_current_records AS current
                LEFT JOIN otlp_span_records AS archive
                    ON archive.trace_id = current.trace_id AND archive.span_id = current.span_id
                """
            )
        ]
    finally:
        connection.close()


def _checkpoint_states(database_path: Path, session_id: str) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(database_path)
    try:
        return {
            str(subject_id): json.loads(state_json)
            for subject_id, state_json in connection.execute(
                "SELECT subject_id, state_json FROM trajectory_retention_checkpoints WHERE session_id = ?",
                (session_id,),
            )
        }
    finally:
        connection.close()


def test_retention_removes_only_the_oldest_contiguous_run_of_expired_turns(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _write(database_path, [
        _record(trace="A", span="turn-1", turn="t1", number=1, created_at=1, start=100),
        _record(trace="A", span="turn-1-child", turn="t1", number=1, parent="turn-1", created_at=1, start=110),
        _record(trace="B", span="turn-2", turn="t2", number=2, created_at=5_000, start=200),
        _record(trace="C", span="turn-3", turn="t3", number=3, created_at=1, start=300),
    ])

    # Turn 3 expired as well, but turn 2 has not, and a page never leaves a hole.
    assert _retire(database_path, now=_DAY_SECONDS + 100) == 2

    assert _span_names(database_path) == ["turn-2", "turn-3"]
    state = _checkpoint_states(database_path, _SESSION_ID)["main"]
    assert state["turns"] == {"max_number": 1, "unnumbered": 0, "trace_turn_ids": {}}
    test_logger.info("retention stopped at the first turn still inside the window")


def test_a_running_record_holds_back_its_turn_and_every_later_one(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        store.write_records([
            _record(trace="A", span="turn-1", turn="t1", number=1, created_at=1, start=100),
            _record(trace="A", span="turn-1-tool", turn="t1", parent="turn-1", created_at=1, start=110, running=True),
            _record(trace="B", span="turn-2", turn="t2", number=2, created_at=1, start=200),
        ])
        # The writer that holds the running record retires nothing behind it.
        assert store.delete_expired(now=_DAY_SECONDS + 1_000) == 0
    finally:
        store.close()

    assert _span_names(database_path) == ["turn-1", "turn-1-tool", "turn-2"]
    test_logger.info("a turn still running blocked every later turn")


def test_a_turn_shared_by_subagents_goes_only_when_every_subject_may_lose_it(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    researcher = ("subagent:researcher", "Researcher", "subagent", "main")
    _write(database_path, [
        _record(trace="A", span="turn-1", turn="t1", number=1, created_at=1, start=100),
        _record(trace="A", span="researcher-1", turn="t1", parent="turn-1", subject=researcher,
                created_at=5_000, start=110),
        _record(trace="B", span="turn-2", turn="t2", number=2, created_at=1, start=200),
    ])

    # The subagent wrote into turn 1 inside the window, so turn 1 stays for
    # the main agent too, and turn 2 cannot go ahead of it.
    assert _retire(database_path, now=_DAY_SECONDS + 100) == 0
    assert _retire(database_path, now=_DAY_SECONDS + 10_000) == 3
    test_logger.info("a turn shared across subjects retired only once every subject allowed it")


def test_team_members_retire_their_own_turns_and_keep_work_outside_turns(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    alice = ("member:alice", "Alice", "team_member")
    bob = ("member:bob", "Bob", "team_member")
    team = "team.work.normal"
    _write(database_path, [
        _record(trace="T", span="alice-setup", parent="root", subject=alice, mode=team, created_at=1, start=50),
        _record(trace="T", span="alice-1", turn="a1", number=1, parent="root", subject=alice, mode=team,
                created_at=1, start=100),
        _record(trace="T", span="bob-1", turn="b1", number=1, parent="root", subject=bob, mode=team,
                created_at=1, start=110),
        _record(trace="T", span="alice-2", turn="a2", number=2, parent="root", subject=alice, mode=team,
                created_at=1, start=200),
        _record(trace="T", span="bob-2", turn="b2", number=2, parent="root", subject=bob, mode=team,
                created_at=5_000, start=210),
        _record(trace="T", span="root", mode=team, created_at=1, start=0),
    ])

    assert _retire(database_path, now=_DAY_SECONDS + 100) == 3

    # Alice's setup ran outside every turn of a trace that still holds Bob's
    # turn, so it stays; the root has no subject and stays with its trace.
    assert _span_names(database_path) == ["alice-setup", "bob-2", "root"]
    states = _checkpoint_states(database_path, _SESSION_ID)
    assert states["member:alice"]["turns"]["trace_turn_ids"] == {_hex("T", 32): ["a1", "a2"]}
    assert states["member:alice"]["turns"]["max_number"] == 2
    assert states["member:bob"]["turns"]["max_number"] == 1

    # Once every turn of the trace is gone, what it held outside them goes too.
    assert _retire(database_path, now=_DAY_SECONDS + 10_000) == 3
    assert _span_names(database_path) == []
    states = _checkpoint_states(database_path, _SESSION_ID)
    assert states["member:alice"]["turns"]["trace_turn_ids"] == {}
    assert states["member:alice"]["turns"]["unnumbered"] == 1
    test_logger.info("team members retired per their own turns, then with their trace")


def _single_agent_store(database_path: Path) -> None:
    _write(database_path, single_agent_records())


def test_repeated_retention_accumulates_checkpoints(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _single_agent_store(database_path)

    assert _retire(database_path, retention_now(1)) > 0
    first = _checkpoint_states(database_path, SINGLE_SESSION_ID)
    assert _retire(database_path, retention_now(2)) > 0
    second = _checkpoint_states(database_path, SINGLE_SESSION_ID)

    main_key = f"{SINGLE_SESSION_ID}\u0000main"
    assert first["main"]["requests"] == {main_key: 2}
    assert second["main"]["requests"] == {main_key: 4}
    assert (first["main"]["turns"]["max_number"], second["main"]["turns"]["max_number"]) == (1, 2)
    # Turn two commits two request windows, then a compaction and its window.
    assert (first["main"]["v2"]["main"]["event_count"], second["main"]["v2"]["main"]["event_count"]) == (2, 6)
    assert second["main"]["v2"]["main"]["held"] is not None
    assert second["main"]["usage"]["input"] == first["main"]["usage"]["input"] + 300 + 400
    # A retired subject keeps its checkpoint, and a schema-v1 subject keeps the
    # request its next request is compared against.
    assert second["subagent:researcher-1"] == first["subagent:researcher-1"]
    coder_lineage = second["subagent:coder"]["lineage"]
    assert [seed["handled_by_v2"] for seed in coder_lineage] == [False]
    assert second["main"]["subject"]["first_observed_time_unix_nano"] == (
        first["main"]["subject"]["first_observed_time_unix_nano"]
    )
    test_logger.info("a second retention pass continued from the first checkpoint")


def _reachable_content(connection: sqlite3.Connection, head: str) -> bool:
    cursor: str | None = head
    depth = 0
    while cursor is not None:
        node = connection.execute(
            "SELECT prev_hash, blob_hash FROM trajectory_sequences WHERE seq_hash = ?",
            (cursor,),
        ).fetchone()
        if node is None:
            return False
        blob = connection.execute(
            "SELECT 1 FROM trajectory_blobs WHERE blob_hash = ?",
            (node[1],),
        ).fetchone()
        if blob is None:
            return False
        cursor = node[0]
        depth += 1
    return depth > 0


def test_retention_keeps_referenced_content_and_reclaims_the_rest(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _single_agent_store(database_path)
    # The tool call the first request answered with is stated by that request
    # alone, which retention removes.
    retired_message = json.dumps(
        {
            "role": "assistant",
            "parts": [{"type": "tool_call", "id": "call-1", "name": "search", "arguments": {"q": "notes"}}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    retired_blob = hashlib.sha256(retired_message).hexdigest()

    _retire(database_path, retention_now(2))

    connection = sqlite3.connect(database_path)
    try:
        heads = [
            str(row[0])
            for row in connection.execute("SELECT DISTINCT seq_hash FROM trajectory_sequence_refs")
        ]
        record_owners = {
            str(row[0])
            for row in connection.execute(
                "SELECT owner_id FROM trajectory_sequence_refs WHERE owner_kind = 'record'"
            )
        }
        current = {
            f"{row[0]}:{row[1]}"
            for row in connection.execute("SELECT trace_id, span_id FROM trajectory_current_records")
        }
        orphan_nodes = connection.execute(
            """
            SELECT COUNT(*) FROM trajectory_blobs
            WHERE blob_hash NOT IN (SELECT blob_hash FROM trajectory_sequences)
            """
        ).fetchone()[0]
        retired_blob_left = connection.execute(
            "SELECT COUNT(*) FROM trajectory_blobs WHERE blob_hash = ?",
            (retired_blob,),
        ).fetchone()[0]
    finally:
        connection.close()
    checkpoint_heads = [
        head
        for state in _checkpoint_states(database_path, SINGLE_SESSION_ID).values()
        for head in checkpoint_sequence_heads(state)
    ]

    connection = sqlite3.connect(database_path)
    try:
        assert checkpoint_heads
        assert all(_reachable_content(connection, head) for head in [*heads, *checkpoint_heads])
    finally:
        connection.close()
    assert record_owners <= current
    assert orphan_nodes == 0
    assert retired_blob_left == 0
    test_logger.info("retention kept every reachable chain and reclaimed retired content")


@pytest.mark.asyncio
async def test_retention_leaves_cumulative_usage_unchanged(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _single_agent_store(database_path)
    reader = AsyncTrajectoryReader(database_path)
    before, _epoch = await reader.get_session_request_usage(SINGLE_SESSION_ID)

    _retire(database_path, retention_now(2))

    after, _epoch = await reader.get_session_request_usage(SINGLE_SESSION_ID)
    before_by_request = {(item["trace_id"], item["inference_id"]): item for item in before}
    assert 0 < len(after) < len(before)
    for item in after:
        assert item == before_by_request[(item["trace_id"], item["inference_id"])]
    test_logger.info("cumulative usage continued from the retired requests")


def test_retention_rotates_the_epoch_and_shrinks_the_file(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _single_agent_store(database_path)
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        epoch = store.fetch_store_epoch()
        connection = store._require_connection()
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        pages_before = connection.execute("PRAGMA page_count").fetchone()[0]
    finally:
        store.close()
    size_before = database_path.stat().st_size

    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        assert store.delete_expired(now=retention_now(4)) > 0
        connection = store._require_connection()
        assert store.fetch_store_epoch() != epoch
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
        pages_after = connection.execute("PRAGMA page_count").fetchone()[0]
    finally:
        store.close()

    assert pages_after < pages_before
    assert database_path.stat().st_size < size_before
    test_logger.info("retention rotated the epoch and handed freed pages back")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (1.0, 1),
        (1.5, None),
        ("1", 1),
        ("  7 ", 7),
        (" 0x4 ", 4),
        ("0b11", 3),
        ("-4", -4),
        ("", 0),
        ("1.0", None),
        ("1_0", None),
        ("-0x4", None),
        (True, 1),
        (False, 0),
        (None, None),
        (9007199254740993, 9007199254740992),
        (float("inf"), None),
    ],
)
def test_integers_read_the_way_the_viewer_reads_them(value: Any, expected: int | None) -> None:
    assert js_bigint(value) == expected
    test_logger.info("BigInt(%r) read as %r", value, expected)


def test_only_records_the_viewer_projects_belong_to_a_turn() -> None:
    trace_id, span_id = "a" * 32, "b" * 16
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "tool.search",
        "attributes": [
            {"key": "openjiuwen.turn.id", "value": {"stringValue": "t1"}},
            {"key": "openjiuwen.turn.number", "value": {"intValue": True}},
        ],
    }

    def payload(*spans: dict[str, Any], **extra: Any) -> bytes:
        document = {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}], **extra}
        return json.dumps(document).encode("utf-8")

    deep: list[Any] = []
    for _ in range(300):
        deep = [deep]
    projected = record_view_facts(payload(span), trace_id, span_id)
    assert (projected.projected, projected.subject_id, projected.turn_id, projected.turn_number) == (
        True,
        "main",
        "t1",
        1,
    )
    listed = record_view_facts(payload({**span, "spanId": "c" * 16}), trace_id, span_id)
    assert (listed.projected, listed.subject_id) == (False, "main")
    for unprojectable in (
        payload(span, span),
        payload(span, nested=deep),
        b'{"resourceSpans": [',
        b'{"resourceSpans": [], "note": "\xff"}',
        b'{"foo": 1}',
    ):
        facts = record_view_facts(unprojectable, trace_id, span_id)
        assert (facts.projected, facts.subject_id) == (False, "__unassigned__")
    test_logger.info("records the viewer cannot project were kept out of every turn")


@pytest.mark.asyncio
async def test_edge_case_records_retire_by_the_viewer_rules(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    _write(database_path, quirk_records())
    reader = AsyncTrajectoryReader(database_path)
    usage, _epoch = await reader.get_session_request_usage(QUIRKS_SESSION_ID)
    # Only int64 token counts the viewer can represent are counted: a double,
    # a string, a negative count and one beyond 2^53 are not.
    main_usage = [item["usage"] for item in usage if item["subject_id"] == "main"]
    assert main_usage[:4] == [{"output": 1}, {"input": 20, "output": 2, "total": 22}, {}, {"output": 4}]

    for removed_turns in (1, 2, 3):
        assert _retire(database_path, retention_now(removed_turns)) > 0
    states = _checkpoint_states(database_path, QUIRKS_SESSION_ID)
    # Turn 1 ended on 3, turn 2 stated 3 and turn 3 a padded hexadecimal 4.
    assert states["main"]["turns"] == {"max_number": 4, "unnumbered": 0, "trace_turn_ids": {}}
    # The earliest observation of helper-y was a record the viewer only lists.
    helper = states["subagent:helper-y"]["subject"]
    assert (helper["display_name"], helper["projected"]) == ("Helper", True)
    assert helper["first_observed_time_unix_nano"] == "1760000000050000000"

    assert _retire(database_path, retention_now(4)) > 0
    states = _checkpoint_states(database_path, QUIRKS_SESSION_ID)
    assert states["main"]["turns"]["unnumbered"] == 1
    assert states["subagent:helper-x"]["subject"]["display_name"] == "Helper"
    unparsable = [payload for payload in _raw_payloads(database_path) if payload == b'{"resourceSpans": [']
    # The unparsable record of turn 1 went with its trace; turn 5's remains.
    assert len(unparsable) == 1
    test_logger.info("edge-case records were retired and checkpointed by the viewer's rules")


def test_committed_retention_fixtures_are_current() -> None:
    with tempfile.TemporaryDirectory() as directory:
        fixtures = build_fixtures(Path(directory))
    assert fixtures
    for file_name, content in fixtures.items():
        committed = (FIXTURE_DIRECTORY / file_name).read_bytes()
        assert committed == content, (
            f"{file_name} is stale; regenerate it with "
            "python tests/unit_tests/observability/retention_fixture_builder.py"
        )
    test_logger.info("the cross-language retention fixtures match the store")
