from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.todo_output import TODO_SNAPSHOT_MODE
from jiuwenswarm.common.todo_snapshot import (
    format_todos_for_frontend,
    load_todo_snapshot_for_frontend,
)


def test_format_todos_keeps_completed_and_drops_cancelled():
    items = [
        SimpleNamespace(id="a", content="pending", activeForm="doing", status=TodoStatus.PENDING),
        SimpleNamespace(id="b", content="done", activeForm="done", status=TodoStatus.COMPLETED),
        SimpleNamespace(id="c", content="cancel", activeForm="cancel", status=TodoStatus.CANCELLED),
    ]

    assert [item["id"] for item in format_todos_for_frontend(items)] == ["a", "b"]


def test_format_todos_defaults_malformed_status_to_pending():
    items = [
        {"id": "dict", "content": "dict status", "status": {}},
        {"id": "list", "content": "list status", "status": []},
        {"id": "none", "content": "none status", "status": None},
    ]

    assert [item["status"] for item in format_todos_for_frontend(items)] == [
        "pending",
        "pending",
        "pending",
    ]


def test_load_snapshot_accepts_json_and_rejects_unsafe_session(tmp_path, monkeypatch):
    root = tmp_path / "todo"
    snapshot = root / "web_session_1" / "todo.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps([{"id": "t1", "content": "restore", "status": "in_progress"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir", lambda: root
    )

    assert load_todo_snapshot_for_frontend("web_session_1") == [{
        "id": "t1",
        "content": "restore",
        "activeForm": "restore",
        "status": "in_progress",
    }]
    assert load_todo_snapshot_for_frontend("../web_session_1") is None


def test_legacy_snapshot_keeps_generation_filter_and_serial_display(tmp_path):
    root = tmp_path / "tenant" / "agent" / "jiuwenclaw_workspace" / "todo"
    snapshot = root / "web_session_2" / "todo.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps([
            {
                "id": "old",
                "content": "old generation",
                "status": "completed",
                "generation_token": "old-token",
            },
            {
                "id": "current-open",
                "content": "current open",
                "status": "in_progress",
                "generation_token": "current-token",
            },
            {
                "id": "current-late",
                "content": "completed out of order",
                "status": "completed",
                "generation_token": "current-token",
            },
        ]),
        encoding="utf-8",
    )

    restored = load_todo_snapshot_for_frontend(
        "web_session_2",
        todo_root=root,
        generation_token="current-token",
    )

    assert [item["id"] for item in restored] == ["current-open", "current-late"]
    assert [item["status"] for item in restored] == ["in_progress", "pending"]
    assert restored.todo_meta is None


@pytest.mark.parametrize("mode", ["legacy", "versioned"])
def test_empty_and_read_failure_are_distinct(tmp_path, mode):
    path = tmp_path / "s" / "todo.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"plan_id": "new", "revision": 7, "todos": []}))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        snapshot = load_todo_snapshot_for_frontend("s", todo_root=tmp_path)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert snapshot == []
    assert snapshot.todo_meta == ({"plan_id": "new", "revision": 7} if mode == "versioned" else None)
    path.write_text("broken")
    assert load_todo_snapshot_for_frontend("s", todo_root=tmp_path) is None
    path.write_text("42")
    assert load_todo_snapshot_for_frontend("s", todo_root=tmp_path) is None
