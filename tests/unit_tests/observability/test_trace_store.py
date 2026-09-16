# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for lossless trajectory persistence and asynchronous reads."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import multiprocessing
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.observability.models import TraceRecordData
from jiuwenswarm.observability.store import (
    AsyncTrajectoryReader,
    TrajectoryCursorError,
    TrajectoryStore,
    decode_revision_cursor,
    decode_trace_cursor,
    encode_revision_cursor,
    encode_trace_cursor,
)

test_logger = logging.getLogger("tests.trajectory_store")

_TRACE_ID = "1" * 32
_SECOND_TRACE_ID = "2" * 32
_ROOT_SPAN_ID = "a" * 16
_CHILD_SPAN_ID = "b" * 16
_THIRD_SPAN_ID = "c" * 16


def _raw_record(
    trace_id: str,
    span_id: str,
    *,
    parent_span_id: str = "",
    name: str = "agent.run",
    status_code: str = "STATUS_CODE_UNSET",
) -> bytes:
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "scope": {"name": "openjiuwen"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "parentSpanId": parent_span_id,
                                    "name": name,
                                    "startTimeUnixNano": "100",
                                    "endTimeUnixNano": "200",
                                    "status": {"code": status_code},
                                    "attributes": [],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _core_record(
    *,
    trace_id: str = _TRACE_ID,
    span_id: str = _ROOT_SPAN_ID,
    parent_span_id: str | None = None,
    session_id: str | None = "session-1",
    request_id: str | None = "request-1",
    run_id: str | None = "run-1",
    agent_mode: str | None = "agent.work.normal",
    start_time: int = 100,
    end_time: int = 200,
    raw_json: bytes | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        raw_json=raw_json
        if raw_json is not None
        else _raw_record(
            trace_id,
            span_id,
            parent_span_id=parent_span_id or "",
        ),
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        start_time_unix_nano=start_time,
        end_time_unix_nano=end_time,
        session_id=session_id,
        request_id=request_id,
        run_id=run_id,
        agent_mode=agent_mode,
        schema_version="1",
    )


def _stored_record(**overrides: object) -> TraceRecordData:
    return TraceRecordData.from_core_record(_core_record(**overrides))


def _team_raw_record(trace_id: str, span_id: str) -> bytes:
    payload = json.loads(_raw_record(trace_id, span_id))
    attributes = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
    attributes.append({
        "key": "openjiuwen.team.id",
        "value": {"stringValue": "research-team"},
    })
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _snapshot_record_for(
    span_id: str,
    revision: int,
    *,
    name: str,
) -> TraceRecordData:
    raw_json = _raw_record(_TRACE_ID, span_id, name=name).replace(
        b',"endTimeUnixNano":"200"',
        b"",
    )
    snapshot = SimpleNamespace(
        raw_json=raw_json,
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        start_time_unix_nano=100,
        observed_time_unix_nano=100 + revision,
        record_revision=revision,
        update_kind="stream_chunk",
        session_id="session-1",
        request_id="request-1",
        run_id="run-1",
        agent_mode="agent.work.normal",
        schema_version="1",
        lifecycle="running",
    )
    return TraceRecordData.from_core_snapshot(snapshot)


def _snapshot_record(revision: int, *, name: str) -> TraceRecordData:
    return _snapshot_record_for(_ROOT_SPAN_ID, revision, name=name)


def _detail_span_id(record: dict[str, Any]) -> str:
    return str(
        record["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
    )


def _opaque_cursor(payload: dict[str, Any]) -> str:
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw_payload).decode("ascii").rstrip("=")


def _write_with_open_wal(database_path: str, ready: Any, release: Any) -> None:
    store = TrajectoryStore(Path(database_path))
    store.initialize()
    try:
        store.write_records([_stored_record()])
        ready.set()
        release.wait(timeout=10)
    finally:
        store.close()


def test_store_preserves_exact_raw_and_records_hash_conflict(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    original_raw = _raw_record(_TRACE_ID, _ROOT_SPAN_ID, name="first")
    conflicting_raw = _raw_record(_TRACE_ID, _ROOT_SPAN_ID, name="second")
    store.initialize()
    try:
        first = store.write_records([_stored_record(raw_json=original_raw)])
        replay = store.write_records([_stored_record(raw_json=original_raw)])
        conflict = store.write_records([_stored_record(raw_json=conflicting_raw)])

        assert first.inserted == 1
        assert replay.inserted == 0
        assert replay.conflicts == 0
        assert conflict.inserted == 0
        assert conflict.conflicts == 1
        assert store.count_conflicts() == 1
        assert store.fetch_raw(_TRACE_ID, _ROOT_SPAN_ID) == original_raw
        assert store.fetch_raw_sha256(
            _TRACE_ID,
            _ROOT_SPAN_ID,
        ) == hashlib.sha256(original_raw).hexdigest()
    finally:
        store.close()
    test_logger.info("raw bytes preserved across replay and conflict")


@pytest.mark.asyncio
async def test_live_revisions_finalize_in_place_and_reject_late_running(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        first = store.write_records([_snapshot_record(1, name="running-1")])
        newest = store.write_records([_snapshot_record(3, name="running-3")])
        stale = store.write_records([_snapshot_record(2, name="running-2")])
        final_record = _stored_record(raw_json=_raw_record(_TRACE_ID, _ROOT_SPAN_ID, name="final"))
        final = store.write_records([final_record])
        late = store.write_records([_snapshot_record(4, name="late-running")])

        connection = store._require_connection()
        current = connection.execute(
            """
            SELECT lifecycle, record_revision, raw_json
            FROM trajectory_current_records
            WHERE trace_id = ? AND span_id = ?
            """,
            (_TRACE_ID, _ROOT_SPAN_ID),
        ).fetchone()
        change_count = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM trajectory_changes
            WHERE trace_id = ? AND span_id = ?
            """,
            (_TRACE_ID, _ROOT_SPAN_ID),
        ).fetchone()
        journal_payload_bytes = connection.execute(
            """
            SELECT COALESCE(SUM(LENGTH(raw_json)), 0) AS payload_bytes
            FROM trajectory_changes
            WHERE trace_id = ? AND span_id = ?
            """,
            (_TRACE_ID, _ROOT_SPAN_ID),
        ).fetchone()
    finally:
        store.close()

    assert first.inserted == 1
    assert newest.inserted == 1
    assert stale.inserted == 0
    assert final.inserted == 1
    assert late.inserted == 0
    assert current is not None
    assert current["lifecycle"] == "final"
    assert bytes(current["raw_json"]) == final_record.raw_json
    assert change_count is not None and int(change_count["count"]) == 3
    assert journal_payload_bytes is not None
    assert int(journal_payload_bytes["payload_bytes"]) == 0

    detail = await AsyncTrajectoryReader(database_path).get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=100,
    )
    assert detail is not None
    assert [record["lifecycle"] for record in detail["records"]] == ["final"]
    assert {record["record_id"] for record in detail["records"]} == {
        f"{_TRACE_ID}:{_ROOT_SPAN_ID}"
    }
    assert await AsyncTrajectoryReader(database_path).get_raw_record(
        "session-1",
        _TRACE_ID,
        _ROOT_SPAN_ID,
    ) == final_record.raw_json
    assert detail["next_since_revision"] == detail["revision"]
    test_logger.info("detail sync returned only the authoritative final identity")


@pytest.mark.asyncio
async def test_detail_delta_coalesces_missed_revisions_but_keeps_live_progress(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_snapshot_record(1, name="running-1")])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    running = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=100,
    )
    assert running is not None
    assert [record["lifecycle"] for record in running["records"]] == ["running"]

    store.initialize()
    try:
        store.write_records([_snapshot_record(2, name="running-2")])
        final_record = _stored_record(
            raw_json=_raw_record(_TRACE_ID, _ROOT_SPAN_ID, name="final"),
        )
        store.write_records([final_record])
    finally:
        store.close()

    final = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=running["next_since_revision"],
        limit=100,
    )
    assert final is not None
    assert len(final["records"]) == 1
    assert final["records"][0]["lifecycle"] == "final"
    assert final["records"][0]["record_revision"] == 1
    assert final["next_since_revision"] == final["revision"]
    test_logger.info("missed running snapshots collapsed while live and final states remained visible")


@pytest.mark.asyncio
async def test_detail_delta_does_not_skip_concurrent_updates_across_pages(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([
            _snapshot_record_for(_ROOT_SPAN_ID, 1, name="root-running-1"),
            _snapshot_record_for(_CHILD_SPAN_ID, 1, name="child-running-1"),
        ])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    first_page = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1,
    )
    assert first_page is not None
    assert first_page["has_more"] is True
    assert [_detail_span_id(record) for record in first_page["records"]] == [
        _ROOT_SPAN_ID
    ]

    store.initialize()
    try:
        store.write_records([
            _snapshot_record_for(_ROOT_SPAN_ID, 2, name="root-running-2"),
            _snapshot_record_for(_CHILD_SPAN_ID, 2, name="child-running-2"),
        ])
    finally:
        store.close()

    cursor = first_page["next_since_revision"]
    observed: list[tuple[str, int]] = []
    while True:
        page = await reader.get_trace_records(
            "session-1",
            _TRACE_ID,
            since_revision=cursor,
            limit=1,
        )
        assert page is not None
        observed.extend(
            (_detail_span_id(record), int(record["record_revision"]))
            for record in page["records"]
        )
        assert page["next_since_revision"] > cursor
        cursor = page["next_since_revision"]
        if not page["has_more"]:
            assert cursor == page["revision"]
            break

    assert observed == [(_ROOT_SPAN_ID, 2), (_CHILD_SPAN_ID, 2)]
    test_logger.info("concurrent updates to returned and pending identities converged without skips")


@pytest.mark.asyncio
async def test_store_preserves_multiple_step_request_spans_and_real_timing(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    step_one_span_id = "d" * 16
    request_one_span_id = "e" * 16
    step_two_span_id = "f" * 16
    request_two_span_id = "9" * 16
    specifications = (
        (_ROOT_SPAN_ID, None, "turn-request", 100, 600, "agent.run"),
        (step_one_span_id, _ROOT_SPAN_ID, "turn-request", 200, 350, "agent.iteration"),
        (request_one_span_id, step_one_span_id, "model-request-1", 250, 330, "llm.chat"),
        (step_two_span_id, _ROOT_SPAN_ID, "turn-request", 400, 560, "agent.iteration"),
        (request_two_span_id, step_two_span_id, "model-request-2", 450, 540, "llm.chat"),
    )
    records: list[TraceRecordData] = []
    for span_id, parent_span_id, request_id, start_time, end_time, name in specifications:
        raw_payload = json.loads(
            _raw_record(
                _TRACE_ID,
                span_id,
                parent_span_id=parent_span_id or "",
                name=name,
            )
        )
        raw_span = raw_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        raw_span["startTimeUnixNano"] = str(start_time)
        raw_span["endTimeUnixNano"] = str(end_time)
        raw_span["attributes"] = [
            {
                "key": "openjiuwen.request.id",
                "value": {"stringValue": request_id},
            }
        ]
        records.append(
            _stored_record(
                span_id=span_id,
                parent_span_id=parent_span_id,
                request_id=request_id,
                start_time=start_time,
                end_time=end_time,
                raw_json=json.dumps(
                    raw_payload,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        )

    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        result = store.write_records(records)
        connection = store._require_connection()
        current_rows = connection.execute(
            """
            SELECT span_id, parent_span_id, request_id, start_time_unix_nano
            FROM trajectory_current_records
            WHERE trace_id = ?
            ORDER BY start_time_unix_nano ASC
            """,
            (_TRACE_ID,),
        ).fetchall()
        change_rows = connection.execute(
            """
            SELECT span_id, parent_span_id, request_id, start_time_unix_nano
            FROM trajectory_changes
            WHERE trace_id = ?
            ORDER BY start_time_unix_nano ASC
            """,
            (_TRACE_ID,),
        ).fetchall()
    finally:
        store.close()

    expected = {
        span_id: (parent_span_id, request_id, start_time)
        for span_id, parent_span_id, request_id, start_time, _end_time, _name in specifications
    }
    assert result.inserted == len(specifications)
    assert {
        str(row["span_id"]): (
            row["parent_span_id"],
            row["request_id"],
            int(row["start_time_unix_nano"]),
        )
        for row in current_rows
    } == expected
    assert {
        str(row["span_id"]): (
            row["parent_span_id"],
            row["request_id"],
            int(row["start_time_unix_nano"]),
        )
        for row in change_rows
    } == expected

    detail = await AsyncTrajectoryReader(database_path).get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=100,
    )
    assert detail is not None
    assert len(detail["records"]) == len(specifications)
    detail_spans = {
        str(record["span_id"]): record["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        for record in detail["records"]
    }
    assert set(detail_spans) == set(expected)
    for span_id, (parent_span_id, request_id, start_time) in expected.items():
        span = detail_spans[span_id]
        assert span.get("parentSpanId", "") == (parent_span_id or "")
        assert int(span["startTimeUnixNano"]) == start_time
        assert span["attributes"] == [
            {
                "key": "openjiuwen.request.id",
                "value": {"stringValue": request_id},
            }
        ]
    assert detail_spans[request_one_span_id]["parentSpanId"] == step_one_span_id
    assert detail_spans[request_two_span_id]["parentSpanId"] == step_two_span_id
    test_logger.info("multiple iteration requests preserved independent identity, parent, and timing")


def test_store_restart_marks_unfinished_snapshot_abandoned(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    store.write_records([_snapshot_record(2, name="unfinished")])
    store.close()

    reopened = TrajectoryStore(database_path)
    reopened.initialize()
    try:
        connection = reopened._require_connection()
        recovered = connection.execute(
            """
            SELECT lifecycle, end_time_unix_nano
            FROM trajectory_current_records
            WHERE trace_id = ? AND span_id = ?
            """,
            (_TRACE_ID, _ROOT_SPAN_ID),
        ).fetchone()
        finalized = reopened.write_records([_stored_record()])
        terminal = connection.execute(
            """
            SELECT lifecycle
            FROM trajectory_current_records
            WHERE trace_id = ? AND span_id = ?
            """,
            (_TRACE_ID, _ROOT_SPAN_ID),
        ).fetchone()
    finally:
        reopened.close()

    assert recovered is not None
    assert recovered["lifecycle"] == "abandoned"
    assert int(recovered["end_time_unix_nano"]) == 0
    assert finalized.inserted == 1
    assert terminal is not None and terminal["lifecycle"] == "final"
    test_logger.info("restart preserved unfinished evidence without inventing an end time")


@pytest.mark.asyncio
async def test_store_reconciles_orphan_without_rewriting_raw(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    child_raw = _raw_record(
        _TRACE_ID,
        _CHILD_SPAN_ID,
        parent_span_id=_ROOT_SPAN_ID,
        name="llm.chat",
    )
    root_raw = _raw_record(_TRACE_ID, _ROOT_SPAN_ID)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    span_id=_CHILD_SPAN_ID,
                    parent_span_id=_ROOT_SPAN_ID,
                    session_id=None,
                    request_id=None,
                    run_id=None,
                    start_time=110,
                    end_time=150,
                    raw_json=child_raw,
                )
            ]
        )
        store.write_records([_stored_record(raw_json=root_raw)])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    items, next_cursor = await reader.list_traces("session-1", limit=30, cursor=None)
    detail = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1000,
    )
    child_after_reconcile = await reader.get_raw_record(
        "session-1",
        _TRACE_ID,
        _CHILD_SPAN_ID,
    )

    assert next_cursor is None
    assert len(items) == 1
    assert items[0]["span_count"] == 2
    assert detail is not None
    assert len(detail["records"]) == 2
    assert child_after_reconcile == child_raw
    test_logger.info("orphan reconciled with raw bytes unchanged")


@pytest.mark.asyncio
async def test_store_reconciles_late_orphan_from_existing_root(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    root_raw = _raw_record(_TRACE_ID, _ROOT_SPAN_ID)
    child_raw = _raw_record(
        _TRACE_ID,
        _CHILD_SPAN_ID,
        parent_span_id=_ROOT_SPAN_ID,
        name="llm.chat",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_stored_record(raw_json=root_raw)])
        store.write_records(
            [
                _stored_record(
                    span_id=_CHILD_SPAN_ID,
                    parent_span_id=_ROOT_SPAN_ID,
                    session_id=None,
                    request_id=None,
                    run_id=None,
                    start_time=110,
                    end_time=150,
                    raw_json=child_raw,
                )
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    items, _cursor = await reader.list_traces("session-1", limit=30, cursor=None)
    child_after_reconcile = await reader.get_raw_record(
        "session-1",
        _TRACE_ID,
        _CHILD_SPAN_ID,
    )

    assert len(items) == 1
    assert items[0]["span_count"] == 2
    assert child_after_reconcile == child_raw
    test_logger.info("late orphan inherited the existing trace session hint")


@pytest.mark.asyncio
async def test_reader_paginates_traces_and_flags_error_status(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    error_raw = _raw_record(
        _SECOND_TRACE_ID,
        _ROOT_SPAN_ID,
        status_code="STATUS_CODE_ERROR",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=_TRACE_ID,
                    start_time=100,
                    end_time=200,
                ),
                _stored_record(
                    trace_id=_SECOND_TRACE_ID,
                    start_time=300,
                    end_time=400,
                    raw_json=error_raw,
                ),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    first_page, cursor = await reader.list_traces("session-1", limit=1, cursor=None)
    second_page, final_cursor = await reader.list_traces(
        "session-1",
        limit=1,
        cursor=cursor,
    )

    assert len(first_page) == 1
    assert first_page[0]["trace_id"] == _SECOND_TRACE_ID
    assert first_page[0]["has_error"] is True
    assert cursor is not None
    assert len(second_page) == 1
    assert second_page[0]["trace_id"] == _TRACE_ID
    assert final_cursor is None
    test_logger.info("cursor pagination preserved newest-first trace ordering")


@pytest.mark.asyncio
async def test_detail_pagination_never_skips_non_monotonic_span_times(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    span_id=_ROOT_SPAN_ID,
                    start_time=300,
                    raw_json=_raw_record(_TRACE_ID, _ROOT_SPAN_ID),
                ),
                _stored_record(
                    span_id=_CHILD_SPAN_ID,
                    start_time=100,
                    raw_json=_raw_record(_TRACE_ID, _CHILD_SPAN_ID),
                ),
                _stored_record(
                    span_id=_THIRD_SPAN_ID,
                    start_time=200,
                    raw_json=_raw_record(_TRACE_ID, _THIRD_SPAN_ID),
                ),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    first_page = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=2,
    )
    assert first_page is not None
    second_page = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=first_page["next_since_revision"],
        limit=2,
    )

    assert second_page is not None
    span_ids = [
        _detail_span_id(record)
        for record in [*first_page["records"], *second_page["records"]]
    ]
    assert span_ids == [_ROOT_SPAN_ID, _CHILD_SPAN_ID, _THIRD_SPAN_ID]
    assert first_page["has_more"] is True
    assert second_page["has_more"] is False
    assert second_page["next_since_revision"] == second_page["revision"]
    test_logger.info("ingest-sequence continuation returned every record once")


@pytest.mark.asyncio
async def test_gateway_reader_sees_committed_wal_from_writer_process(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_write_with_open_wal,
        args=(str(database_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=10)
        reader = AsyncTrajectoryReader(database_path)
        items, cursor = await reader.list_traces("session-1", limit=30, cursor=None)
        raw = await reader.get_raw_record("session-1", _TRACE_ID, _ROOT_SPAN_ID)

        assert cursor is None
        assert len(items) == 1
        assert raw == _raw_record(_TRACE_ID, _ROOT_SPAN_ID)
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert process.exitcode == 0
    test_logger.info("Gateway read committed WAL data while the writer process remained open")


@pytest.mark.asyncio
async def test_malformed_raw_is_stored_and_exposed_without_projection(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    malformed_raw = b"{not-valid-json"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_stored_record(raw_json=malformed_raw)])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    detail = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1000,
    )
    raw = await reader.get_raw_record("session-1", _TRACE_ID, _ROOT_SPAN_ID)

    assert detail is not None
    assert detail["records"][0]["raw_valid"] is False
    assert detail["records"][0]["otlp"] is None
    assert raw == malformed_raw
    test_logger.info("malformed JSON retained for raw diagnostics")


@pytest.mark.asyncio
async def test_reader_accepts_agent_and_team_modes_but_rejects_unknown_traces(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    mixed_trace_id = "5" * 32
    unknown_trace_id = "6" * 32
    team_trace_id = "7" * 32
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=mixed_trace_id,
                    span_id="1" * 16,
                    agent_mode="agent.work.normal",
                    raw_json=_raw_record(mixed_trace_id, "1" * 16),
                ),
                _stored_record(
                    trace_id=mixed_trace_id,
                    span_id="2" * 16,
                    agent_mode="team.plan.normal",
                    raw_json=_raw_record(mixed_trace_id, "2" * 16),
                ),
                _stored_record(
                    trace_id=unknown_trace_id,
                    span_id="3" * 16,
                    agent_mode=None,
                    raw_json=_raw_record(unknown_trace_id, "3" * 16),
                ),
                _stored_record(
                    trace_id=team_trace_id,
                    span_id="4" * 16,
                    agent_mode="team",
                    raw_json=_raw_record(team_trace_id, "4" * 16),
                ),
                _stored_record(),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    items, _cursor = await reader.list_traces("session-1", limit=30, cursor=None)

    assert [item["trace_id"] for item in items] == [
        _TRACE_ID,
        team_trace_id,
        mixed_trace_id,
    ]
    for rejected_trace_id, span_id in (
        (unknown_trace_id, "3" * 16),
    ):
        assert await reader.get_trace_records(
            "session-1",
            rejected_trace_id,
            since_revision=0,
            limit=1000,
        ) is None
        assert await reader.get_raw_record(
            "session-1",
            rejected_trace_id,
            span_id,
        ) is None
    assert await reader.get_trace_records(
        "session-1",
        team_trace_id,
        since_revision=0,
        limit=1000,
    ) is not None
    assert await reader.get_trace_records(
        "session-1",
        mixed_trace_id,
        since_revision=0,
        limit=1000,
    ) is not None
    test_logger.info("trace-level allowlist accepted Agent and Team while rejecting unknown modes")


@pytest.mark.asyncio
async def test_initialize_repairs_mode_less_team_trace_from_raw_contract(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([
            _stored_record(
                agent_mode=None,
                raw_json=_team_raw_record(_TRACE_ID, _ROOT_SPAN_ID),
            ),
            _stored_record(
                span_id=_CHILD_SPAN_ID,
                parent_span_id=_ROOT_SPAN_ID,
                agent_mode=None,
                raw_json=_raw_record(
                    _TRACE_ID,
                    _CHILD_SPAN_ID,
                    parent_span_id=_ROOT_SPAN_ID,
                    name="llm.call",
                ),
            ),
        ])
    finally:
        store.close()

    before, _cursor = await AsyncTrajectoryReader(database_path).list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )
    assert before == []

    reopened = TrajectoryStore(database_path)
    reopened.initialize()
    reopened.close()

    after, _cursor = await AsyncTrajectoryReader(database_path).list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )
    assert [item["trace_id"] for item in after] == [_TRACE_ID]
    with sqlite3.connect(database_path) as connection:
        for table in (
            "otlp_span_records",
            "trajectory_current_records",
            "trajectory_changes",
        ):
            modes = connection.execute(
                f"SELECT DISTINCT agent_mode FROM {table} WHERE trace_id = ?",
                (_TRACE_ID,),
            ).fetchall()
            assert modes == [("team",)]
    test_logger.info("startup repaired a legacy mode-less Team trace without rewriting raw OTLP")


@pytest.mark.asyncio
async def test_detail_byte_budget_uses_index_descriptor_and_preserves_raw(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    first_raw = _raw_record(_TRACE_ID, _ROOT_SPAN_ID)
    large_raw = _raw_record(_TRACE_ID, _CHILD_SPAN_ID).replace(
        b'"attributes":[]',
        b'"attributes":[{"key":"padding","value":{"stringValue":"'
        + b"x" * 2048
        + b'"}}]',
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(raw_json=first_raw),
                _stored_record(
                    span_id=_CHILD_SPAN_ID,
                    parent_span_id=_ROOT_SPAN_ID,
                    raw_json=large_raw,
                ),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    first_page = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1000,
        max_bytes=len(first_raw) + 16,
    )
    assert first_page is not None
    second_page = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=first_page["next_since_revision"],
        limit=1000,
        max_bytes=len(first_raw) + 16,
    )

    assert first_page["has_more"] is True
    assert len(first_page["records"]) == 1
    assert first_page["projected_raw_bytes"] == len(first_raw)
    assert second_page is not None
    assert second_page["has_more"] is False
    assert second_page["next_since_revision"] == second_page["revision"]
    assert second_page["records"] == [
        {
            "ingest_seq": second_page["revision"],
            "change_seq": second_page["revision"],
            "record_id": f"{_TRACE_ID}:{_CHILD_SPAN_ID}",
            "trace_id": _TRACE_ID,
            "span_id": _CHILD_SPAN_ID,
            "record_revision": 1,
            "lifecycle": "final",
            "operation": "upsert",
            "observed_time_unix_nano": "200",
            "raw_size_bytes": len(large_raw),
            "otlp": None,
            "raw_valid": None,
            "projection_omitted": "record_too_large",
        }
    ]
    assert await reader.get_raw_record(
        "session-1",
        _TRACE_ID,
        _CHILD_SPAN_ID,
    ) == large_raw
    test_logger.info("oversize detail advanced by index while raw remained exact")


@pytest.mark.asyncio
async def test_detail_strict_json_rejects_non_otlp_and_non_finite_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    invalid_records = (
        (_ROOT_SPAN_ID, b"NaN"),
        (_CHILD_SPAN_ID, b"[]"),
        (_THIRD_SPAN_ID, b'{"resourceSpans":NaN}'),
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(span_id=span_id, raw_json=raw_json)
                for span_id, raw_json in invalid_records
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    detail = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=1000,
    )

    assert detail is not None
    assert len(detail["records"]) == 3
    assert all(record["otlp"] is None for record in detail["records"])
    assert all(record["raw_valid"] is False for record in detail["records"])
    test_logger.info("strict detail projection rejected NaN and non-OTLP JSON")


@pytest.mark.asyncio
async def test_trace_list_cursor_is_stable_when_root_arrives_late(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    oldest_trace_id = "8" * 32
    middle_trace_id = "9" * 32
    newest_trace_id = "a" * 32
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=oldest_trace_id,
                    span_id="5" * 16,
                    start_time=50,
                    raw_json=_raw_record(oldest_trace_id, "5" * 16),
                ),
                _stored_record(
                    trace_id=middle_trace_id,
                    span_id="6" * 16,
                    parent_span_id="7" * 16,
                    start_time=100,
                    raw_json=_raw_record(middle_trace_id, "6" * 16),
                ),
                _stored_record(
                    trace_id=newest_trace_id,
                    span_id="8" * 16,
                    start_time=200,
                    raw_json=_raw_record(newest_trace_id, "8" * 16),
                ),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    first_page, cursor = await reader.list_traces(
        "session-1",
        limit=2,
        cursor=None,
    )
    assert [item["trace_id"] for item in first_page] == [
        newest_trace_id,
        middle_trace_id,
    ]
    assert cursor is not None

    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=middle_trace_id,
                    span_id="7" * 16,
                    start_time=10,
                    raw_json=_raw_record(middle_trace_id, "7" * 16),
                )
            ]
        )
    finally:
        store.close()

    second_page, final_cursor = await reader.list_traces(
        "session-1",
        limit=2,
        cursor=cursor,
    )
    assert [item["trace_id"] for item in second_page] == [oldest_trace_id]
    assert final_cursor is None
    test_logger.info("late root did not move a trace across the list cursor")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_trace_count", "expected_page_sizes"),
    [
        (31, [30, 2]),
        (61, [30, 30, 2]),
    ],
)
async def test_revision_feed_pages_new_traces_and_late_old_revision(
    tmp_path: Path,
    new_trace_count: int,
    expected_page_sizes: list[int],
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_stored_record()])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    initial_items, _list_cursor, revision_cursor, store_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )
    initial_revision = initial_items[0]["revision"]

    new_trace_ids = [f"{1000 + index:032x}" for index in range(new_trace_count)]
    new_records = [
        _stored_record(
            trace_id=trace_id,
            span_id=f"{1000 + index:016x}",
            start_time=1000 + index,
            end_time=2000 + index,
            raw_json=_raw_record(trace_id, f"{1000 + index:016x}"),
        )
        for index, trace_id in enumerate(new_trace_ids)
    ]
    late_span_id = "f" * 16
    new_records.append(
        _stored_record(
            span_id=late_span_id,
            parent_span_id=_ROOT_SPAN_ID,
            start_time=300,
            end_time=400,
            raw_json=_raw_record(
                _TRACE_ID,
                late_span_id,
                parent_span_id=_ROOT_SPAN_ID,
            ),
        )
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(new_records)
    finally:
        store.close()

    cursor = revision_cursor
    page_sizes: list[int] = []
    watermarks: set[str] = set()
    summaries_by_trace_id: dict[str, dict[str, Any]] = {}
    while True:
        (
            items,
            next_cursor,
            watermark,
            has_more,
            reset,
            revision_epoch,
        ) = await reader.list_trace_revisions(
            "session-1", after_revision=cursor, limit=30
        )
        page_sizes.append(len(items))
        watermarks.add(watermark)
        assert reset is False
        assert revision_epoch == store_epoch
        for item in items:
            summaries_by_trace_id[item["trace_id"]] = item
        assert next_cursor
        if not has_more:
            assert next_cursor == watermark
            cursor = next_cursor
            break
        assert next_cursor != cursor
        cursor = next_cursor

    assert page_sizes == expected_page_sizes
    assert len(watermarks) == 1
    assert set(new_trace_ids).issubset(summaries_by_trace_id)
    assert summaries_by_trace_id[_TRACE_ID]["span_count"] == 2
    assert summaries_by_trace_id[_TRACE_ID]["revision"] > initial_revision

    unchanged, stable_cursor, stable_watermark, has_more, reset, revision_epoch = (
        await reader.list_trace_revisions(
            "session-1",
            after_revision=cursor,
            limit=30,
        )
    )
    assert unchanged == []
    assert stable_cursor == cursor
    assert stable_watermark == cursor
    assert has_more is False
    assert reset is False
    assert revision_epoch == store_epoch
    test_logger.info(
        "revision feed found %d new traces and one late old-trace revision",
        new_trace_count,
    )


@pytest.mark.asyncio
async def test_revision_feed_continuation_keeps_first_page_watermark(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_stored_record()])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, baseline, store_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )
    first_batch_trace_ids = [f"{2000 + index:032x}" for index in range(2)]
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=trace_id,
                    span_id=f"{2000 + index:016x}",
                    raw_json=_raw_record(trace_id, f"{2000 + index:016x}"),
                )
                for index, trace_id in enumerate(first_batch_trace_ids)
            ]
        )
    finally:
        store.close()

    (
        first_items,
        continuation,
        watermark,
        has_more,
        reset,
        revision_epoch,
    ) = await reader.list_trace_revisions(
        "session-1", after_revision=baseline, limit=1
    )
    assert has_more is True
    assert reset is False
    assert revision_epoch == store_epoch

    later_trace_id = "e" * 32
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=later_trace_id,
                    span_id="e" * 16,
                    raw_json=_raw_record(later_trace_id, "e" * 16),
                )
            ]
        )
    finally:
        store.close()

    (
        second_items,
        completed_cursor,
        repeated_watermark,
        has_more,
        reset,
        revision_epoch,
    ) = (
        await reader.list_trace_revisions(
            "session-1",
            after_revision=continuation,
            limit=1,
        )
    )
    assert has_more is False
    assert reset is False
    assert revision_epoch == store_epoch
    assert completed_cursor == watermark
    assert repeated_watermark == watermark
    assert {item["trace_id"] for item in [*first_items, *second_items]} == set(
        first_batch_trace_ids
    )

    (
        next_items,
        next_cursor,
        next_watermark,
        has_more,
        reset,
        revision_epoch,
    ) = await reader.list_trace_revisions(
        "session-1", after_revision=completed_cursor, limit=1
    )
    assert [item["trace_id"] for item in next_items] == [later_trace_id]
    assert next_cursor == next_watermark
    assert has_more is False
    assert reset is False
    assert revision_epoch == store_epoch
    test_logger.info("revision pagination deferred concurrent commits to the next poll")


@pytest.mark.asyncio
async def test_store_epoch_persists_and_database_replacement_resets_cursor(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    backup_path = tmp_path / "trajectory-backup.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        first_epoch = store.fetch_store_epoch()
        store.write_records([_stored_record()])
        assert store.fetch_store_epoch() == first_epoch
    finally:
        store.close()

    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        assert store.fetch_store_epoch() == first_epoch
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, old_cursor, reader_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )
    assert reader_epoch == first_epoch

    database_path.replace(backup_path)
    replacement_store = TrajectoryStore(database_path)
    replacement_store.initialize()
    try:
        replacement_epoch = replacement_store.fetch_store_epoch()
    finally:
        replacement_store.close()

    assert replacement_epoch != first_epoch
    items, cursor, watermark, has_more, reset, current_epoch = (
        await reader.list_trace_revisions(
            "session-1",
            after_revision=old_cursor,
            limit=30,
        )
    )
    assert items == []
    assert cursor == watermark
    assert has_more is False
    assert reset is True
    assert current_epoch == replacement_epoch
    assert decode_revision_cursor(cursor) == (
        "session-1",
        replacement_epoch,
        0,
        None,
    )
    test_logger.info("database replacement minted a new persistent epoch and reset")


@pytest.mark.asyncio
async def test_ingest_sequence_rollback_rotates_epoch_and_resets_cursor(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(),
                _stored_record(
                    span_id=_CHILD_SPAN_ID,
                    raw_json=_raw_record(_TRACE_ID, _CHILD_SPAN_ID),
                ),
            ]
        )
        old_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, old_cursor, listed_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )
    assert listed_epoch == old_epoch

    rollback_connection = sqlite3.connect(str(database_path))
    try:
        rollback_connection.execute(
            "DELETE FROM otlp_span_records WHERE ingest_seq = 2"
        )
        rollback_connection.commit()
    finally:
        rollback_connection.close()

    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        new_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    assert new_epoch != old_epoch
    revisions = await reader.list_trace_revisions(
        "session-1",
        after_revision=old_cursor,
        limit=30,
    )
    assert revisions[0] == []
    assert revisions[4] is True
    assert revisions[5] == new_epoch
    assert decode_revision_cursor(revisions[1]) == (
        "session-1",
        new_epoch,
        1,
        None,
    )
    test_logger.info("ingest sequence rollback minted a new epoch and reset")


@pytest.mark.asyncio
async def test_retention_deletion_rotates_epoch_and_resets_revision_feed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    expired_record = TraceRecordData.from_core_record(_core_record(), created_at=1)
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        store.write_records([expired_record])
        old_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, old_cursor, listed_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )
    assert listed_epoch == old_epoch

    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        assert store.delete_expired(now=86402) == 1
        new_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    assert new_epoch != old_epoch
    items, cursor, watermark, has_more, reset, current_epoch = (
        await reader.list_trace_revisions(
            "session-1",
            after_revision=old_cursor,
            limit=30,
        )
    )
    assert items == []
    assert cursor == watermark
    assert has_more is False
    assert reset is True
    assert current_epoch == new_epoch
    assert decode_revision_cursor(cursor)[2:] == (0, None)
    test_logger.info("retention deletion rotated epoch before returning an empty view")


@pytest.mark.asyncio
async def test_partial_retention_rotates_epoch_and_rebuilds_remaining_view(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    expired = TraceRecordData.from_core_record(_core_record(), created_at=1)
    retained = TraceRecordData.from_core_record(
        _core_record(
            span_id=_CHILD_SPAN_ID,
            raw_json=_raw_record(_TRACE_ID, _CHILD_SPAN_ID),
        ),
        created_at=100,
    )
    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        store.write_records([expired, retained])
        old_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, old_cursor, _listed_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )

    store = TrajectoryStore(database_path, retention_days=1)
    store.initialize()
    try:
        assert store.delete_expired(now=86402) == 1
        new_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    assert new_epoch != old_epoch
    revisions = await reader.list_trace_revisions(
        "session-1",
        after_revision=old_cursor,
        limit=30,
    )
    assert revisions[0] == []
    assert revisions[4] is True
    assert revisions[5] == new_epoch
    remaining, _cursor = await reader.list_traces(
        "session-1",
        limit=30,
        cursor=None,
    )
    assert len(remaining) == 1
    assert remaining[0]["span_count"] == 1
    assert remaining[0]["revision"] == 2
    test_logger.info("partial retention forced a full rebuild of the remaining view")


@pytest.mark.asyncio
async def test_global_trace_eligibility_accepts_team_and_keeps_sessions_isolated(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_stored_record()])
        old_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, old_cursor, _listed_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )

    team_span_id = "d" * 16
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    span_id=team_span_id,
                    session_id="session-2",
                    agent_mode="team.plan.normal",
                    raw_json=_raw_record(_TRACE_ID, team_span_id),
                )
            ]
        )
        new_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    assert new_epoch == old_epoch
    session_one_items, _cursor = await reader.list_traces(
        "session-1", limit=30, cursor=None
    )
    session_two_items, _cursor = await reader.list_traces(
        "session-2", limit=30, cursor=None
    )
    assert len(session_one_items) == 1
    assert len(session_two_items) == 1
    assert await reader.get_trace_records(
        "session-1", _TRACE_ID, since_revision=0, limit=100
    ) is not None
    assert await reader.get_raw_record(
        "session-1", _TRACE_ID, _ROOT_SPAN_ID
    ) is not None
    assert await reader.get_raw_record(
        "session-2", _TRACE_ID, team_span_id
    ) is not None

    revisions = await reader.list_trace_revisions(
        "session-1",
        after_revision=old_cursor,
        limit=30,
    )
    assert revisions[4] is False
    assert revisions[5] == new_epoch
    test_logger.info("Agent and Team records sharing a trace ID remained session isolated")


@pytest.mark.asyncio
async def test_global_eligibility_keeps_record_paths_session_isolated(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    shared_trace_id = "3" * 32
    first_span_id = "3" * 16
    second_span_id = "4" * 16
    first_raw = _raw_record(shared_trace_id, first_span_id)
    second_raw = _raw_record(shared_trace_id, second_span_id)
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=shared_trace_id,
                    span_id=first_span_id,
                    session_id="session-1",
                    raw_json=first_raw,
                ),
                _stored_record(
                    trace_id=shared_trace_id,
                    span_id=second_span_id,
                    session_id="session-2",
                    raw_json=second_raw,
                ),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    first_items, _cursor = await reader.list_traces(
        "session-1", limit=30, cursor=None
    )
    second_items, _cursor = await reader.list_traces(
        "session-2", limit=30, cursor=None
    )
    assert first_items[0]["span_count"] == 1
    assert second_items[0]["span_count"] == 1
    assert await reader.get_raw_record(
        "session-1", shared_trace_id, first_span_id
    ) == first_raw
    assert await reader.get_raw_record(
        "session-1", shared_trace_id, second_span_id
    ) is None
    assert await reader.get_raw_record(
        "session-2", shared_trace_id, first_span_id
    ) is None
    assert await reader.get_raw_record(
        "session-2", shared_trace_id, second_span_id
    ) == second_raw
    test_logger.info("global eligibility did not broaden session-scoped record paths")


@pytest.mark.asyncio
async def test_newly_eligible_trace_preserves_epoch_and_updates_revision_feed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    unknown_span_id = "5" * 16
    known_span_id = "6" * 16
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    span_id=unknown_span_id,
                    agent_mode=None,
                    raw_json=_raw_record(_TRACE_ID, unknown_span_id),
                )
            ]
        )
        old_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    items, _list_cursor, old_cursor, listed_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=30,
            cursor=None,
        )
    )
    assert items == []
    assert listed_epoch == old_epoch

    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    span_id=known_span_id,
                    agent_mode="agent.work.normal",
                    raw_json=_raw_record(_TRACE_ID, known_span_id),
                )
            ]
        )
        new_epoch = store.fetch_store_epoch()
    finally:
        store.close()

    assert new_epoch == old_epoch
    visible_items, _cursor = await reader.list_traces(
        "session-1", limit=30, cursor=None
    )
    assert visible_items[0]["span_count"] == 2
    revisions = await reader.list_trace_revisions(
        "session-1", after_revision=old_cursor, limit=30
    )
    assert len(revisions[0]) == 1
    assert revisions[0][0]["trace_id"] == _TRACE_ID
    assert revisions[0][0]["span_count"] == 2
    assert revisions[4] is False
    assert revisions[5] == old_epoch
    test_logger.info("newly eligible trace stayed on the incremental revision feed")


@pytest.mark.asyncio
async def test_raw_first_mixed_batch_preserves_unprojectable_blobs(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    deep_raw = b'{"resourceSpans":' + b"[" * 2000 + b"]" * 2000 + b"}"
    long_integer_raw = (
        b'{"resourceSpans":[],"value":' + b"9" * 10000 + b"}"
    )
    invalid_utf8_raw = b'{"resourceSpans":[],"value":"\xff"}'
    valid_raw = _raw_record(_TRACE_ID, _ROOT_SPAN_ID)
    raw_by_span_id = {
        _ROOT_SPAN_ID: valid_raw,
        _CHILD_SPAN_ID: deep_raw,
        _THIRD_SPAN_ID: long_integer_raw,
        "d" * 16: invalid_utf8_raw,
    }
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        result = store.write_records(
            [
                _stored_record(span_id=span_id, raw_json=raw_json)
                for span_id, raw_json in raw_by_span_id.items()
            ]
        )
        assert result.inserted == len(raw_by_span_id)
        for span_id, raw_json in raw_by_span_id.items():
            assert store.fetch_raw(_TRACE_ID, span_id) == raw_json
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    detail = await reader.get_trace_records(
        "session-1",
        _TRACE_ID,
        since_revision=0,
        limit=100,
    )
    assert detail is not None
    projected_by_span_id = {
        str(record["span_id"]): record for record in detail["records"]
    }
    assert projected_by_span_id[_ROOT_SPAN_ID]["raw_valid"] is True
    for span_id in (_CHILD_SPAN_ID, _THIRD_SPAN_ID, "d" * 16):
        assert projected_by_span_id[span_id]["raw_valid"] is False
        assert await reader.get_raw_record(
            "session-1", _TRACE_ID, span_id
        ) == raw_by_span_id[span_id]
    test_logger.info("deep, huge-integer, and invalid-UTF8 blobs survived one batch")


@pytest.mark.asyncio
async def test_cursor_scope_range_and_canonical_encoding(tmp_path: Path) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(),
                _stored_record(
                    trace_id=_SECOND_TRACE_ID,
                    span_id=_CHILD_SPAN_ID,
                    raw_json=_raw_record(_SECOND_TRACE_ID, _CHILD_SPAN_ID),
                ),
            ]
        )
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, list_cursor, revision_cursor, store_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1",
            limit=1,
            cursor=None,
        )
    )
    assert list_cursor is not None
    decoded_list_cursor = decode_trace_cursor(list_cursor)
    assert decoded_list_cursor[:2] == ("session-1", store_epoch)
    assert decode_revision_cursor(revision_cursor)[:2] == (
        "session-1",
        store_epoch,
    )

    for invalid_cursor in (
        f"{revision_cursor}!!!",
        f"{revision_cursor}=",
        f" {revision_cursor}",
        _opaque_cursor(
            {
                "v": 2,
                "s": "session-1",
                "e": store_epoch,
                "a": str(1 << 63),
            }
        ),
        _opaque_cursor(
            {
                "v": 2,
                "s": "session-1",
                "e": store_epoch,
                "a": "01",
            }
        ),
    ):
        with pytest.raises(TrajectoryCursorError):
            decode_revision_cursor(invalid_cursor)

    for invalid_cursor in (
        f"{list_cursor}!!!",
        f"{list_cursor}=",
        f"{list_cursor} ",
    ):
        with pytest.raises(TrajectoryCursorError):
            decode_trace_cursor(invalid_cursor)

    with pytest.raises(TrajectoryCursorError):
        encode_revision_cursor("session-1", store_epoch, 1 << 63)
    with pytest.raises(TrajectoryCursorError):
        encode_trace_cursor("session-1", store_epoch, 1 << 63, _TRACE_ID)
    with pytest.raises(TrajectoryCursorError):
        await reader.list_traces(
            "session-2",
            limit=1,
            cursor=list_cursor,
        )
    with pytest.raises(TrajectoryCursorError):
        await reader.list_traces(
            "session-1",
            limit=1,
            cursor=encode_trace_cursor(
                "session-1",
                store_epoch,
                (1 << 63) - 1,
                _TRACE_ID,
            ),
        )

    for stale_cursor in (
        encode_revision_cursor("session-2", store_epoch, 0),
        encode_revision_cursor("session-1", "stale-epoch", 0),
        encode_revision_cursor("session-1", store_epoch, (1 << 63) - 1),
    ):
        revisions = await reader.list_trace_revisions(
            "session-1",
            after_revision=stale_cursor,
            limit=30,
        )
        assert revisions[0] == []
        assert revisions[4] is True
        assert revisions[5] == store_epoch
    test_logger.info("cursor scope, signed range, and canonical base64url were enforced")


@pytest.mark.asyncio
async def test_revision_summary_is_frozen_at_first_page_watermark(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trajectory.sqlite3"
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records([_stored_record()])
    finally:
        store.close()

    reader = AsyncTrajectoryReader(database_path)
    _items, _list_cursor, baseline, store_epoch = (
        await reader.list_traces_with_revision_cursor(
            "session-1", limit=30, cursor=None
        )
    )
    first_trace_id = "8" * 32
    second_trace_id = "9" * 32
    first_span_id = "8" * 16
    second_span_id = "9" * 16
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=first_trace_id,
                    span_id=first_span_id,
                    start_time=300,
                    end_time=400,
                    raw_json=_raw_record(first_trace_id, first_span_id),
                ),
                _stored_record(
                    trace_id=second_trace_id,
                    span_id=second_span_id,
                    start_time=500,
                    end_time=600,
                    raw_json=_raw_record(second_trace_id, second_span_id),
                ),
            ]
        )
    finally:
        store.close()

    first_page = await reader.list_trace_revisions(
        "session-1", after_revision=baseline, limit=1
    )
    assert [item["trace_id"] for item in first_page[0]] == [first_trace_id]
    assert first_page[3] is True
    assert first_page[4] is False

    late_span_id = "a" * 16
    error_raw = _raw_record(
        second_trace_id,
        late_span_id,
        status_code="STATUS_CODE_ERROR",
    )
    store = TrajectoryStore(database_path)
    store.initialize()
    try:
        store.write_records(
            [
                _stored_record(
                    trace_id=second_trace_id,
                    span_id=late_span_id,
                    request_id="zz-request",
                    run_id="zz-run",
                    start_time=900,
                    end_time=1000,
                    raw_json=error_raw,
                )
            ]
        )
        assert store.fetch_store_epoch() == store_epoch
    finally:
        store.close()

    second_page = await reader.list_trace_revisions(
        "session-1",
        after_revision=first_page[1],
        limit=1,
    )
    assert second_page[3] is False
    assert second_page[4] is False
    assert second_page[2] == first_page[2]
    frozen_summary = second_page[0][0]
    assert frozen_summary["trace_id"] == second_trace_id
    assert frozen_summary["span_count"] == 1
    assert frozen_summary["end_time_unix_nano"] == 600
    assert frozen_summary["request_id"] == "request-1"
    assert frozen_summary["has_error"] is False

    next_poll = await reader.list_trace_revisions(
        "session-1",
        after_revision=second_page[1],
        limit=1,
    )
    updated_summary = next_poll[0][0]
    assert updated_summary["trace_id"] == second_trace_id
    assert updated_summary["span_count"] == 2
    assert updated_summary["end_time_unix_nano"] == 1000
    assert updated_summary["request_id"] == "zz-request"
    assert updated_summary["run_id"] == "zz-run"
    assert updated_summary["has_error"] is True
    test_logger.info("revision summary aggregation stayed below the frozen watermark")
