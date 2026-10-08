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
