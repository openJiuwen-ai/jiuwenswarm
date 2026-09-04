# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
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
    # 每批为了精确判断 has_more 会重读一条 lookahead 记录；其余字节只扫一次。
    max_record_bytes = max(
        len((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        for record in records
    )
    batch_count = (len(records) + 16) // 17
    assert total_scanned <= file_size + batch_count * (max_record_bytes + 4096)


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


def test_cursor_detects_atomic_rewrite(tmp_path, monkeypatch):
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
