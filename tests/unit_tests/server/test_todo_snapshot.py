# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import json
from types import SimpleNamespace

from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.common.todo_snapshot import (
    format_todos_for_frontend,
    load_todo_snapshot_for_frontend,
    session_uses_project_todo_dir,
)


def test_format_todos_for_frontend_keeps_completed_and_cancelled():
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
        {
            "id": "c",
            "content": "cancelled task",
            "activeForm": "canceling",
            "status": "cancelled",
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
        },
        {
            "id": "day2",
            "content": "灵隐",
            "activeForm": "规划灵隐",
            "status": "cancelled",
        },
    ]


def test_format_todos_for_frontend_drops_deleted():
    raw = [
        {
            "id": "gone",
            "content": "deleted task",
            "activeForm": "deleting",
            "status": "deleted",
        },
    ]

    assert format_todos_for_frontend(raw) == []


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


def test_session_uses_project_todo_dir_only_for_single_agent_code():
    assert session_uses_project_todo_dir(
        project_dir="D:/proj",
        work_mode="code",
        mode="agent.code.normal",
    )
    assert not session_uses_project_todo_dir(
        project_dir="D:/proj",
        work_mode="work",
        mode="agent.work.normal",
    )
    assert not session_uses_project_todo_dir(
        project_dir="D:/proj",
        work_mode="code",
        mode="team.code.normal",
    )
    assert not session_uses_project_todo_dir(
        project_dir="",
        work_mode="code",
        mode="agent.code.normal",
    )


def test_load_todo_snapshot_prefers_project_dir_for_code_profile(tmp_path, monkeypatch):
    session_id = "web_code_proj"
    project = tmp_path / "agent-core"
    default_root = tmp_path / "default-todo"
    project_todo = project / "todo" / session_id
    project_todo.mkdir(parents=True)
    (project_todo / "todo.json").write_text(
        json.dumps(
            [{"id": "p1", "content": "from project", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    (default_root / session_id).mkdir(parents=True)
    (default_root / session_id / "todo.json").write_text(
        json.dumps(
            [{"id": "d1", "content": "from default", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: default_root,
    )

    loaded = load_todo_snapshot_for_frontend(
        session_id,
        project_dir=str(project),
        work_mode="code",
        mode="agent.code.normal",
    )
    assert loaded == [
        {"id": "p1", "content": "from project", "activeForm": "doing", "status": "pending"}
    ]


def test_load_todo_snapshot_ignores_project_dir_for_work_profile(tmp_path, monkeypatch):
    session_id = "web_work_proj"
    project = tmp_path / "work-proj"
    default_root = tmp_path / "default-todo"
    project_todo = project / "todo" / session_id
    project_todo.mkdir(parents=True)
    (project_todo / "todo.json").write_text(
        json.dumps(
            [{"id": "p1", "content": "project leftover", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    (default_root / session_id).mkdir(parents=True)
    (default_root / session_id / "todo.json").write_text(
        json.dumps(
            [{"id": "d1", "content": "work todos", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: default_root,
    )

    loaded = load_todo_snapshot_for_frontend(
        session_id,
        project_dir=str(project),
        work_mode="work",
        mode="agent.work.normal",
    )
    assert loaded == [
        {"id": "d1", "content": "work todos", "activeForm": "doing", "status": "pending"}
    ]


def test_load_todo_snapshot_team_code_stays_on_default_workspace(tmp_path, monkeypatch):
    session_id = "web_team_code"
    project = tmp_path / "team-proj"
    default_root = tmp_path / "default-todo"
    project_todo = project / "todo" / session_id
    project_todo.mkdir(parents=True)
    (project_todo / "todo.json").write_text(
        json.dumps(
            [{"id": "p1", "content": "project leftover", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    (default_root / session_id).mkdir(parents=True)
    (default_root / session_id / "todo.json").write_text(
        json.dumps(
            [{"id": "d1", "content": "team default", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: default_root,
    )

    loaded = load_todo_snapshot_for_frontend(
        session_id,
        project_dir=str(project),
        work_mode="code",
        mode="team.code.normal",
    )
    assert loaded == [
        {"id": "d1", "content": "team default", "activeForm": "doing", "status": "pending"}
    ]


def test_load_todo_snapshot_falls_back_to_default_when_project_file_missing(
    tmp_path, monkeypatch
):
    session_id = "web_code_fallback"
    project = tmp_path / "agent-core"
    project.mkdir()
    default_root = tmp_path / "default-todo"
    (default_root / session_id).mkdir(parents=True)
    (default_root / session_id / "todo.json").write_text(
        json.dumps(
            [{"id": "d1", "content": "legacy default", "activeForm": "doing", "status": "pending"}]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: default_root,
    )

    loaded = load_todo_snapshot_for_frontend(
        session_id,
        project_dir=str(project),
        work_mode="code",
        mode="agent.code.normal",
    )
    assert loaded == [
        {"id": "d1", "content": "legacy default", "activeForm": "doing", "status": "pending"}
    ]
