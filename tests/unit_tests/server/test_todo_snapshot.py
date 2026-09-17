# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import json
from types import SimpleNamespace

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.todo_snapshot import (
    format_todos_for_frontend,
    load_todo_snapshot_for_frontend,
)


def test_format_todos_for_frontend_keeps_completed_and_drops_cancelled():
    items = [
        SimpleNamespace(
            id="a",
            content="pending task",
            activeForm="doing pending",
            status=TodoStatus.PENDING,
        ),
        SimpleNamespace(
            id="b",
            content="done task",
            activeForm="finishing",
            status=TodoStatus.COMPLETED,
        ),
        SimpleNamespace(
            id="c",
            content="cancelled task",
            activeForm="canceling",
            status=TodoStatus.CANCELLED,
        ),
    ]

    assert format_todos_for_frontend(items) == [
        {
            "id": "a",
            "content": "pending task",
            "activeForm": "doing pending",
            "status": "pending",
        },
        {
            "id": "b",
            "content": "done task",
            "activeForm": "finishing",
            "status": "completed",
        },
    ]


def test_format_todos_for_frontend_accepts_raw_json_dicts():
    raw = [
        {
            "id": "day1",
            "content": "西湖",
            "activeForm": "规划西湖",
            "status": "in_progress",
        },
        {
            "id": "day2",
            "content": "灵隐",
            "activeForm": "规划灵隐",
            "status": "cancelled",
        },
    ]

    assert format_todos_for_frontend(raw) == [
        {
            "id": "day1",
            "content": "西湖",
            "activeForm": "规划西湖",
            "status": "in_progress",
        }
    ]


def test_load_todo_snapshot_for_frontend_reads_workspace_file(tmp_path, monkeypatch):
    session_id = "web_test_session"
    todo_dir = tmp_path / "todo" / session_id
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.json").write_text(
        json.dumps(
            [
                {
                    "id": "t1",
                    "content": "task one",
                    "activeForm": "doing one",
                    "status": "pending",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: tmp_path / "todo",
    )

    assert load_todo_snapshot_for_frontend(session_id) == [
        {
            "id": "t1",
            "content": "task one",
            "activeForm": "doing one",
            "status": "pending",
        }
    ]


def test_load_todo_snapshot_for_frontend_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: tmp_path / "todo",
    )
    assert load_todo_snapshot_for_frontend("missing_session") == []
