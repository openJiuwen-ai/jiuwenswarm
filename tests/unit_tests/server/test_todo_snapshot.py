from __future__ import annotations

import json
from types import SimpleNamespace

from openjiuwen.harness.schema.task import TodoStatus

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
    assert load_todo_snapshot_for_frontend("../web_session_1") == []


def test_load_snapshot_filters_old_generation_and_overlays_serial_statuses(tmp_path):
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
