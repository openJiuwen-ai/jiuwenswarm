# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.common import todo_snapshot as snap


def test_complete_open_todos_marks_pending_and_in_progress(tmp_path, monkeypatch):
    sid = "sess-todo-sync"
    todo_root = tmp_path / "todo"
    session_dir = todo_root / sid
    session_dir.mkdir(parents=True)
    todo_file = session_dir / "todo.json"
    todo_file.write_text(
        json.dumps(
            [
                {
                    "id": "plan",
                    "content": "规划大纲",
                    "activeForm": "规划大纲",
                    "status": "completed",
                },
                {
                    "id": "gen",
                    "content": "生成PPT",
                    "activeForm": "生成PPT",
                    "status": "in_progress",
                },
                {
                    "id": "deliver",
                    "content": "校验并交付",
                    "activeForm": "校验并交付",
                    "status": "pending",
                },
                {
                    "id": "skip",
                    "content": "已取消",
                    "activeForm": "已取消",
                    "status": "cancelled",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(snap, "get_deepagent_todo_dir", lambda: todo_root)

    formatted, changed = snap.complete_open_todos_for_session(sid)
    assert changed == 2
    raw = json.loads(todo_file.read_text(encoding="utf-8"))
    by_id = {row["id"]: row["status"] for row in raw}
    assert by_id["plan"] == "completed"
    assert by_id["gen"] == "completed"
    assert by_id["deliver"] == "completed"
    assert by_id["skip"] == "cancelled"
    assert {t["id"] for t in formatted} == {"plan", "gen", "deliver"}
    assert all(t["status"] == "completed" for t in formatted)


def test_complete_open_todos_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "get_deepagent_todo_dir", lambda: tmp_path / "todo")
    formatted, changed = snap.complete_open_todos_for_session("missing")
    assert formatted == []
    assert changed == 0
