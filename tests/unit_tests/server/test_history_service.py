# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.control.history_service import (
    InvalidHistoryCursor,
    load_history_query,
)
from jiuwenswarm.server.runtime.session import session_history


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(record, ensure_ascii=False)}\n" for record in records),
        encoding="utf-8",
    )


def test_load_history_query_cursor_null_returns_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session_id = "web_1a0ae5020cb_b84cd20ac835"
    _write_jsonl(
        tmp_path / session_id / "history.jsonl",
        [{"id": "m1", "role": "user", "content": "hello"}],
    )

    data = load_history_query(
        {"session_id": session_id, "cursor": None, "limit": 50}
    )

    assert data is not None
    assert data["messages"][0]["content"] == "hello"
    assert data["has_more"] is False
    assert data.get("snapshot_id")


def test_load_history_query_without_page_idx_or_cursor_returns_none():
    assert load_history_query({"session_id": "web_missing"}) is None


def test_load_history_query_rejects_non_string_cursor():
    with pytest.raises(InvalidHistoryCursor, match="null or a string"):
        load_history_query({"session_id": "web_abc", "cursor": 123, "limit": 50})
