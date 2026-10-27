# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import session_history


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(record, ensure_ascii=False)}\n" for record in records),
        encoding="utf-8",
    )


def _all_records(record: dict) -> bool:
    return True


def _read_all_cursor_records(session_id: str, *, limit: int) -> list[dict]:
    restored: list[dict] = []
    cursor = None
    while True:
        page = session_history.read_history_cursor_page(
            session_id,
            cursor=cursor,
            limit=limit,
            is_restorable=_all_records,
        )
        restored.extend(page["messages"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return restored


def test_cursor_reads_snapshot_newest_first_without_rescanning_file(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_large"
    records = [
        {"id": f"r-{index}", "role": "user", "content": "内容" * 800}
        for index in range(120)
    ]
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(path, records)
    file_size = path.stat().st_size

    cursor = None
    restored: list[str] = []
    total_scanned = 0
    while True:
        page = session_history.read_history_cursor_page(
            session_id,
            cursor=cursor,
            limit=17,
            is_restorable=_all_records,
            read_block_bytes=4096,
        )
        restored.extend(record["id"] for record in page["messages"])
        total_scanned += page["scanned_bytes"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break

    assert restored == [f"r-{index}" for index in reversed(range(120))]
    max_record_bytes = max(
        len((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        for record in records
    )
    batch_count = (len(records) + 16) // 17
    assert total_scanned <= file_size + batch_count * (max_record_bytes + 4096)


def test_cursor_dedups_cumulative_final_across_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_final_chain"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [
            {
                "id": "round:assistant",
                "request_id": "round",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "draft",
            },
            {"id": "f1:user", "request_id": "round", "role": "user", "content": "f1"},
            {
                "id": "round:assistant",
                "request_id": "round",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "draft complete",
            },
            {"id": "f2:user", "request_id": "round", "role": "user", "content": "f2"},
        ],
    )

    restored = _read_all_cursor_records(session_id, limit=2)

    assert [record["id"] for record in restored] == [
        "f2:user",
        "round:assistant",
        "f1:user",
    ]
    assert restored[1]["content"] == "draft complete"


def test_cursor_keeps_independent_final_segments_across_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_final_segments"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [
            {
                "id": "round:assistant",
                "request_id": "round",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "first segment",
            },
            {"id": "f1:user", "request_id": "round", "role": "user", "content": "f1"},
            {
                "id": "round:assistant",
                "request_id": "round",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "second segment",
            },
        ],
    )

    restored = _read_all_cursor_records(session_id, limit=2)

    assert [record.get("content") for record in restored] == [
        "second segment",
        "f1",
        "first segment",
    ]


def test_cursor_merges_aborted_final_across_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_aborted"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [
            {
                "id": "round:assistant",
                "request_id": "round",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "answer",
            },
            {"id": "f1:user", "request_id": "round", "role": "user", "content": "f1"},
            {
                "id": "round:assistant",
                "request_id": "round",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "",
                "aborted": True,
                "reasoning_content": "stopped",
            },
        ],
    )

    restored = _read_all_cursor_records(session_id, limit=1)
    answer = next(record for record in restored if record["id"] == "round:assistant")

    assert answer["content"] == "answer"
    assert answer["aborted"] is True
    assert answer["reasoning_content"] == "stopped"
    assert all(record.get("content") != "" for record in restored)


def test_cursor_preserves_standalone_aborted_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_standalone_aborted"
    marker = {
        "id": "round:assistant",
        "request_id": "round",
        "role": "assistant",
        "event_type": "chat.final",
        "content": "",
        "aborted": True,
    }
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(path, [marker])

    restored = _read_all_cursor_records(session_id, limit=50)

    assert restored == [marker]


def test_cursor_drops_resumed_empty_aborted_final(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_resumed_empty_abort"
    records = [
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "",
            "aborted": True,
        },
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "answer after resume",
        },
    ]
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(path, records)

    restored = _read_all_cursor_records(session_id, limit=1)

    assert restored == [records[1]]


def test_cursor_preserves_standalone_aborted_marker_order(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_aborted_order"
    records = [
        {
            "id": "r1:assistant",
            "request_id": "r1",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "old answer",
        },
        {
            "id": "r2:assistant",
            "request_id": "r2",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "",
            "aborted": True,
            "timestamp": 2,
        },
    ]
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(path, records)

    restored = _read_all_cursor_records(session_id, limit=50)

    assert restored == list(reversed(records))


def test_cursor_orders_multiple_standalone_aborted_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_multiple_aborted_order"
    records = [
        {"id": "r0:user", "request_id": "r0", "role": "user", "content": "r0"},
        {
            "id": "a:assistant",
            "request_id": "a",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "",
            "aborted": True,
        },
        {"id": "r1:user", "request_id": "r1", "role": "user", "content": "r1"},
        {
            "id": "b:assistant",
            "request_id": "b",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "",
            "aborted": True,
        },
        {"id": "r2:user", "request_id": "r2", "role": "user", "content": "r2"},
    ]
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(path, records)

    restored = _read_all_cursor_records(session_id, limit=50)

    assert restored == list(reversed(records))


def test_cursor_boundary_inserts_completed_pending_markers_in_reverse_order():
    marker_a = {
        "id": "a:assistant",
        "request_id": "a",
        "event_type": "chat.final",
        "content": "",
        "aborted": True,
    }
    marker_b = {
        "id": "b:assistant",
        "request_id": "b",
        "event_type": "chat.final",
        "content": "",
        "aborted": True,
    }
    records = ["r0", "r1", "r2", "r3", "r4"]
    states = {
        "a:assistant": {
            "request": "a",
            "pending": marker_a,
            "pending_position": 2,
        },
        "b:assistant": {
            "request": "b",
            "pending": marker_b,
            "pending_position": 4,
        },
        "boundary:assistant": {
            "request": "boundary",
            "pending": None,
            "pending_position": None,
        },
    }

    carried = session_history._cursor_state_for_boundary(
        records,
        states,
        "boundary",
        "boundary",
    )

    assert records == ["r0", "r1", marker_a, "r2", "r3", marker_b, "r4"]
    assert list(carried) == ["boundary:assistant"]


def test_cursor_rejects_decompression_bomb(monkeypatch):
    monkeypatch.setattr(
        session_history,
        "_HISTORY_CURSOR_MAX_PAYLOAD_BYTES",
        128,
    )
    compressed = zlib.compress(b'{"v":1,"padding":"' + b"x" * 4096 + b'"}')
    cursor = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")

    with pytest.raises(session_history.InvalidHistoryCursor, match="size limit"):
        session_history._decode_history_cursor(cursor)


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_cursor_matches_legacy_dedup_semantics(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = f"cursor_semantics_{limit}"
    records = [
        {"id": "u1:user", "request_id": "round", "role": "user", "content": "u1"},
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.tool_call",
            "content": "",
            "tool_call": {"tool_call_id": "tool-1"},
        },
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "draft",
        },
        {"id": "u2:user", "request_id": "round", "role": "user", "content": "u2"},
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "draft complete",
        },
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "independent segment",
        },
        {
            "id": "round:assistant",
            "request_id": "round",
            "role": "assistant",
            "event_type": "chat.final",
            "content": "",
            "aborted": True,
        },
    ]
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(path, records)

    restored = _read_all_cursor_records(session_id, limit=limit)
    expected = list(reversed(session_history._dedup_records_last_wins(records)))

    assert restored == expected


def test_cursor_initial_page_on_over_1000_records_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_over_1000"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [
            {"id": f"r-{index}", "role": "user", "content": "x" * 200}
            for index in range(1200)
        ],
    )

    page = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=50,
        is_restorable=_all_records,
    )

    assert len(page["messages"]) == 50
    assert page["messages"][0]["id"] == "r-1199"
    assert page["has_more"] is True
    assert page["next_cursor"] is not None
    assert page["scanned_bytes"] < path.stat().st_size // 2


def test_cursor_initial_read_flushes_queued_history_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    session_id = "cursor_queued_write"

    session_history.append_history_record(
        session_id=session_id,
        request_id="request-1",
        channel_id="desktop",
        role="user",
        content="刚刚写入的问题",
        timestamp=1.0,
    )
    page = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=50,
        is_restorable=_all_records,
    )

    assert [record["content"] for record in page["messages"]] == ["刚刚写入的问题"]


def test_cursor_snapshot_excludes_appends_and_retry_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_append"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [{"id": f"r-{index}", "role": "user", "content": str(index)} for index in range(6)],
    )

    first = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=2,
        is_restorable=_all_records,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "new", "role": "user", "content": "new"}) + "\n")

    second = session_history.read_history_cursor_page(
        session_id,
        cursor=first["next_cursor"],
        limit=2,
        is_restorable=_all_records,
    )
    repeated = session_history.read_history_cursor_page(
        session_id,
        cursor=first["next_cursor"],
        limit=2,
        is_restorable=_all_records,
    )

    assert [record["id"] for record in first["messages"]] == ["r-5", "r-4"]
    assert second == repeated
    assert [record["id"] for record in second["messages"]] == ["r-3", "r-2"]
    assert all(record["id"] != "new" for record in second["messages"])


def test_cursor_skips_non_restorable_records_without_losing_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_filter"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [
            {"id": "keep-1", "keep": True},
            {"id": "skip-1", "keep": False},
            {"id": "skip-2", "keep": False},
            {"id": "keep-2", "keep": True},
            {"id": "skip-3", "keep": False},
            {"id": "keep-3", "keep": True},
        ],
    )

    def predicate(record: dict) -> bool:
        return record.get("keep") is True

    first = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=2,
        is_restorable=predicate,
        read_block_bytes=32,
    )
    second = session_history.read_history_cursor_page(
        session_id,
        cursor=first["next_cursor"],
        limit=2,
        is_restorable=predicate,
        read_block_bytes=32,
    )

    assert [record["id"] for record in first["messages"]] == ["keep-3", "keep-2"]
    assert first["has_more"] is True
    assert [record["id"] for record in second["messages"]] == ["keep-1"]
    assert second["has_more"] is False
    assert second["next_cursor"] is None


def test_cursor_detects_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_rewrite"
    path = tmp_path / session_id / "history.jsonl"
    _write_jsonl(
        path,
        [{"id": f"r-{index}", "role": "user", "content": str(index)} for index in range(4)],
    )
    first = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=1,
        is_restorable=_all_records,
    )

    session_history.write_history_records(session_id, [{"id": "replacement"}])

    with pytest.raises(session_history.HistorySnapshotChanged):
        session_history.read_history_cursor_page(
            session_id,
            cursor=first["next_cursor"],
            limit=1,
            is_restorable=_all_records,
        )


def test_cursor_handles_utf8_and_one_record_larger_than_block(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "cursor_utf8"
    path = tmp_path / session_id / "history.jsonl"
    huge = "汉字🙂" * 100_000
    _write_jsonl(
        path,
        [
            {"id": "first", "role": "user", "content": "开始"},
            {"id": "huge", "role": "assistant", "event_type": "chat.final", "content": huge},
        ],
    )

    page = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=2,
        is_restorable=_all_records,
        read_block_bytes=1024,
    )

    assert [record["id"] for record in page["messages"]] == ["huge", "first"]
    assert page["messages"][0]["content"] == huge


def test_cursor_migrates_legacy_json_once(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.delenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", raising=False)
    session_id = "cursor_legacy"
    legacy = tmp_path / session_id / "history.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps([{"id": "legacy", "role": "user", "content": "ok"}]),
        encoding="utf-8",
    )

    page = session_history.read_history_cursor_page(
        session_id,
        cursor=None,
        limit=50,
        is_restorable=_all_records,
    )

    assert [record["id"] for record in page["messages"]] == ["legacy"]
    assert (legacy.parent / "history.jsonl").exists()


def test_cursor_rejects_malformed_cross_session_and_legacy_env(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    _write_jsonl(
        tmp_path / "cursor_security" / "history.jsonl",
        [{"id": "r-1", "role": "user", "content": "one"}],
    )

    with pytest.raises(session_history.InvalidHistoryCursor, match="malformed"):
        session_history.read_history_cursor_page(
            "cursor_security",
            cursor="not-a-cursor",
            limit=1,
            is_restorable=_all_records,
        )

    cursor = session_history._encode_history_cursor({
        "v": session_history._HISTORY_CURSOR_VERSION,
        "stream": session_history._cursor_stream_key("another_session"),
        "dev": 1,
        "ino": 1,
        "end": 1,
        "pos": 0,
    })
    with pytest.raises(session_history.InvalidHistoryCursor, match="different session"):
        session_history.read_history_cursor_page(
            "cursor_security",
            cursor=cursor,
            limit=1,
            is_restorable=_all_records,
        )

    monkeypatch.setenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", "1")
    legacy = tmp_path / "cursor_legacy_env" / "history.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[]", encoding="utf-8")
    with pytest.raises(session_history.InvalidHistoryCursor, match="requires JSONL"):
        session_history.read_history_cursor_page(
            "cursor_legacy_env",
            cursor=None,
            limit=1,
            is_restorable=_all_records,
        )
