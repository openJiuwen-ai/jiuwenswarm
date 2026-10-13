# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reminders stay listable across turns and stay out of the execution map."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openjiuwen.harness.schema.task import TodoItem, TodoStatus

from jiuwenswarm.agents.harness.common.tools.todo_compat import (
    CompatibleTodoModifyTool,
    merge_persistent_list_items,
    persist_backlog_create,
    restore_persistent_reminders,
)
from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    TURN_USER_QUERY_KEY,
    filter_todos_for_display,
    filter_todos_for_execution,
    is_backlog_todo_query,
    is_persistent_todo,
    remember_turn_user_query,
    snapshot_includes_persistent_todos,
)


def test_backlog_query_detects_add_todo_and_rejects_execute_now() -> None:
    assert is_backlog_todo_query("帮我添加一个待办：明天下午2点提交测试报告")
    assert is_backlog_todo_query("提醒我后天下午交报告")
    assert not is_backlog_todo_query("帮我添加一个待办，现在就做")
    assert not is_backlog_todo_query("帮我做一份测试报告")


def test_execution_filter_drops_reminders_display_keeps_them() -> None:
    items = [
        {
            "id": "reminder",
            "status": "pending",
            "generation_token": "gen-old",
            "meta_data": {"persistent": True},
        },
        {
            "id": "stale_plan",
            "status": "in_progress",
            "generation_token": "gen-old",
        },
        {
            "id": "current_plan",
            "status": "pending",
            "generation_token": "gen-new",
        },
    ]
    assert [item["id"] for item in filter_todos_for_execution(items, "gen-new")] == [
        "current_plan",
    ]
    assert [item["id"] for item in filter_todos_for_display(items, "gen-new")] == [
        "reminder",
        "current_plan",
    ]


def test_snapshot_includes_reminders_only_for_list_or_touched_modify() -> None:
    items = [{"id": "reminder", "meta_data": {"persistent": True}}]
    assert snapshot_includes_persistent_todos("todo_list", {}, items)
    assert snapshot_includes_persistent_todos(
        "todo_modify",
        {"action": "update", "todos": [{"id": "reminder", "content": "later"}]},
        items,
    )
    assert not snapshot_includes_persistent_todos(
        "todo_modify",
        {"action": "update", "todos": [{"id": "stage", "status": "completed"}]},
        items,
    )
    assert snapshot_includes_persistent_todos("todo_create", {}, items)
    assert not snapshot_includes_persistent_todos("todo_create", {}, [])


class _State:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def update_state(self, data: dict[str, Any]) -> None:
        self.store.update(data)

    def get_state(self, key: str) -> Any:
        return self.store.get(key)

    def get_session_id(self) -> str:
        return "sess-1"


class _TodoFile:
    def __init__(self, todos: list[TodoItem]) -> None:
        self.todos = todos

    async def load_todos(self, _session_id: str) -> list[TodoItem]:
        return list(self.todos)

    async def save_todos(self, _session_id: str, todos: list[TodoItem]) -> None:
        self.todos = list(todos)


@pytest.mark.asyncio
async def test_backlog_create_marks_pending_and_drops_execute_now() -> None:
    session = _State()
    remember_turn_user_query(session, "帮我添加一个待办：明天下午2点提交测试报告")
    created = TodoItem(
        id="submit_test_report",
        content="明天下午2点提交测试报告",
        status=TodoStatus.IN_PROGRESS,
        generation_token="gen-1",
    )
    tool = _TodoFile([created])
    result = await persist_backlog_create(
        tool,
        session,
        {"message": "Successfully created 1 task(s):\nNext step: Immediately execute task 'x'"},
    )
    assert tool.todos[0].status == TodoStatus.PENDING
    assert tool.todos[0].meta_data == {"persistent": True}
    assert "Immediately execute" not in result["message"]
    assert "pending" in result["message"]
    assert session.get_state(TURN_USER_QUERY_KEY).startswith("帮我添加一个待办")


@pytest.mark.asyncio
async def test_ordinary_create_keeps_existing_reminder() -> None:
    reminder = TodoItem(
        id="submit_test_report",
        content="明天下午2点提交测试报告",
        status=TodoStatus.PENDING,
        meta_data={"persistent": True},
    )
    plan = TodoItem(id="analyze", content="分析数据", status=TodoStatus.IN_PROGRESS)
    tool = _TodoFile([plan])

    await restore_persistent_reminders(tool, "sess-1", [reminder])
    session = _State()
    remember_turn_user_query(session, "帮我分析这份数据")
    result = await persist_backlog_create(
        tool,
        session,
        {"message": "Successfully created 1 task(s)"},
    )

    assert [todo.id for todo in tool.todos] == ["analyze", "submit_test_report"]
    assert tool.todos[0].status == TodoStatus.IN_PROGRESS
    assert not is_persistent_todo(tool.todos[0])
    assert tool.todos[1].status == TodoStatus.PENDING
    assert is_persistent_todo(tool.todos[1])
    assert "stays listed" not in result["message"]


@pytest.mark.asyncio
async def test_restore_drops_finished_reminder_and_duplicate_id() -> None:
    done = TodoItem(
        id="old",
        content="已完成",
        status=TodoStatus.COMPLETED,
        meta_data={"persistent": True},
    )
    same = TodoItem(
        id="analyze",
        content="分析数据",
        status=TodoStatus.PENDING,
        meta_data={"persistent": True},
    )
    plan = TodoItem(id="analyze", content="分析数据", status=TodoStatus.IN_PROGRESS)
    tool = _TodoFile([plan])

    await restore_persistent_reminders(tool, "sess-1", [done, same])

    assert [todo.id for todo in tool.todos] == ["analyze"]
    assert tool.todos[0].status == TodoStatus.IN_PROGRESS
    assert not is_persistent_todo(tool.todos[0])


@pytest.mark.asyncio
async def test_list_merge_returns_old_generation_reminder() -> None:
    reminder = TodoItem(
        id="submit_test_report",
        content="明天下午2点提交测试报告",
        status=TodoStatus.PENDING,
        generation_token="gen-old",
        meta_data={"persistent": True},
    )
    tool = _TodoFile([reminder])
    merged = await merge_persistent_list_items(tool, _State(), {"tasks": []})
    assert merged["tasks"] == [
        {
            "id": "submit_test_report",
            "content": "明天下午2点提交测试报告",
            "status": "pending",
            "depends_on": [],
        }
    ]


@pytest.mark.asyncio
async def test_modify_to_in_progress_clears_persistent_mark() -> None:
    reminder = TodoItem(
        id="submit_test_report",
        content="提交测试报告",
        status=TodoStatus.PENDING,
        meta_data={"persistent": True},
    )
    saved: list[list[TodoItem]] = []

    tool = object.__new__(CompatibleTodoModifyTool)

    async def save_todos(_session_id: str, todos: list[TodoItem]) -> None:
        saved.append(list(todos))

    tool.save_todos = save_todos  # type: ignore[method-assign]
    tool._validate_single_in_progress = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    await tool._update_todos(  # pylint: disable=protected-access
        "sess-1",
        [{"id": "submit_test_report", "status": "in_progress"}],
        [reminder],
        generation_token="gen-new",
    )
    assert saved[0][0].status == TodoStatus.IN_PROGRESS
    assert saved[0][0].meta_data is None
    assert saved[0][0].generation_token == "gen-new"


def test_rail_execution_load_excludes_persistent(tmp_path: Path) -> None:
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    rail = TaskExecutionRail()
    rail._todo_generation_token = "gen-new"  # pylint: disable=protected-access
    todo_path = tmp_path / "todo.json"
    todo_path.write_text(
        json.dumps(
            [
                {
                    "id": "reminder",
                    "content": "提交测试报告",
                    "status": "pending",
                    "generation_token": "gen-old",
                    "meta_data": {"persistent": True},
                },
                {
                    "id": "current",
                    "content": "当前步骤",
                    "status": "pending",
                    "generation_token": "gen-new",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rail._get_todo_workspace_path = lambda _session_id: todo_path  # type: ignore[method-assign]
    execution = rail._load_todo_from_json("sess")  # pylint: disable=protected-access
    display = rail._load_todo_for_display("sess")  # pylint: disable=protected-access
    assert [item["id"] for item in execution] == ["current"]
    assert [item["id"] for item in display] == ["reminder", "current"]
    rail._todo_map = rail._build_map_from_todo_items(execution)  # pylint: disable=protected-access
    assert rail._pick_task_id_for_binding() == "current"  # pylint: disable=protected-access
