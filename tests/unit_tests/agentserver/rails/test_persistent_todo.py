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
    user_query_drops_all_reminders,
    user_query_drops_reminder,
)


def test_backlog_query_detects_add_todo_and_rejects_execute_now() -> None:
    assert is_backlog_todo_query("帮我添加一个待办：明天下午2点提交测试报告")
    assert is_backlog_todo_query("提醒我后天下午交报告")
    assert not is_backlog_todo_query("帮我添加一个待办，现在就做")
    assert not is_backlog_todo_query("帮我做一份测试报告")


def test_drop_query_needs_a_delete_verb_and_a_reminder_noun() -> None:
    assert user_query_drops_reminder("把这个待办删了")
    assert user_query_drops_reminder("取消这个待办")
    assert user_query_drops_reminder("移除这个待办")
    assert user_query_drops_reminder("把待办清掉")
    assert user_query_drops_reminder("取消那条提醒")
    assert user_query_drops_reminder("不要这条提醒了")
    assert user_query_drops_all_reminders("把这些待办都删掉")
    assert not user_query_drops_all_reminders("把这个待办删了")
    assert not user_query_drops_reminder("我有哪些待办事项？")
    assert not user_query_drops_reminder("取消生成PPT")
    assert not user_query_drops_reminder("帮我分析这份数据")
    assert not user_query_drops_reminder("delete this reminder")


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


def _reminder_and_plan() -> tuple[TodoItem, TodoItem]:
    reminder = TodoItem(
        id="submit_test_report",
        content="明天下午2点提交测试报告",
        status=TodoStatus.PENDING,
        meta_data={"persistent": True},
    )
    plan = TodoItem(id="stage", content="生成PPT", status=TodoStatus.PENDING)
    return reminder, plan


def _modify_tool(todos: list[TodoItem]) -> CompatibleTodoModifyTool:
    tool = object.__new__(CompatibleTodoModifyTool)

    async def load_todos(_session_id: str) -> list[TodoItem]:
        return todos

    async def save_todos(_session_id: str, updated: list[TodoItem]) -> None:
        todos[:] = list(updated)

    tool.load_todos = load_todos  # type: ignore[method-assign]
    tool.save_todos = save_todos  # type: ignore[method-assign]
    return tool


@pytest.mark.asyncio
async def test_unrelated_turn_cannot_cancel_or_delete_reminder() -> None:
    reminder, plan = _reminder_and_plan()
    held = [reminder, plan]
    tool = _modify_tool(held)
    session = _State()
    remember_turn_user_query(session, "我有哪些待办事项？")

    cancelled = await tool.invoke(
        {
            "action": "update",
            "todos": [
                {"id": "submit_test_report", "status": "cancelled"},
                {"id": "stage", "status": "cancelled"},
            ],
        },
        session=session,
    )
    assert reminder.status == TodoStatus.PENDING
    assert is_persistent_todo(reminder)
    assert plan.status == TodoStatus.CANCELLED
    assert "Kept 1 reminder" in cancelled["message"]

    reminder.status = TodoStatus.PENDING
    plan.status = TodoStatus.PENDING
    deleted = await tool.invoke(
        {"action": "delete", "ids": ["submit_test_report", "stage"]},
        session=session,
    )
    assert is_persistent_todo(reminder)
    assert reminder.status == TodoStatus.PENDING
    assert [todo.id for todo in held] == ["submit_test_report"]
    assert "Kept 1 reminder" in deleted["message"]
    assert "stage" in deleted["message"]


@pytest.mark.asyncio
async def test_explicit_delete_query_removes_reminder() -> None:
    reminder, _plan = _reminder_and_plan()
    held = [reminder]
    tool = _modify_tool(held)
    session = _State()
    remember_turn_user_query(session, "把明天下午2点提交测试报告这个待办删了")

    result = await tool.invoke(
        {"action": "delete", "ids": ["submit_test_report"]},
        session=session,
    )
    assert held == []
    assert "Successfully deleted 1" in result["message"]
    assert "Kept" not in result["message"]


@pytest.mark.asyncio
async def test_deleting_a_plan_item_keeps_the_only_reminder() -> None:
    reminder, plan = _reminder_and_plan()
    held = [reminder, plan]
    tool = _modify_tool(held)
    session = _State()
    remember_turn_user_query(session, "把生成PPT这个待办删了")

    result = await tool.invoke(
        {"action": "delete", "ids": ["submit_test_report", "stage"]},
        session=session,
    )

    assert [todo.id for todo in held] == ["submit_test_report"]
    assert reminder.status == TodoStatus.PENDING
    assert is_persistent_todo(reminder)
    assert "Kept 1 reminder" in result["message"]


@pytest.mark.asyncio
async def test_bulk_phrase_deletes_every_reminder() -> None:
    first = _persistent("tomorrow", "明早提醒")
    second = _persistent("last_week", "上周提醒")
    held = [first, second]
    tool = _modify_tool(held)
    session = _State()
    remember_turn_user_query(session, "把这些待办都删掉")

    result = await tool.invoke(
        {"action": "delete", "ids": ["tomorrow", "last_week"]},
        session=session,
    )

    assert held == []
    assert "Kept" not in result["message"]


def _persistent(todo_id: str, content: str) -> TodoItem:
    return TodoItem(
        id=todo_id,
        content=content,
        status=TodoStatus.PENDING,
        meta_data={"persistent": True},
    )


@pytest.mark.asyncio
async def test_bulk_cancel_on_a_delete_turn_keeps_unnamed_reminders() -> None:
    named = _persistent("tomorrow", "明早提醒")
    other = _persistent("last_week", "上周提醒")
    held = [named, other]
    tool = _modify_tool(held)
    session = _State()
    remember_turn_user_query(session, "把这个待办删了，再记一个待办：周五开会")

    result = await tool.invoke(
        {
            "action": "update",
            "todos": [
                {"id": "tomorrow", "status": "cancelled"},
                {"id": "last_week", "status": "cancelled"},
            ],
        },
        session=session,
    )

    assert named.status == TodoStatus.PENDING
    assert other.status == TodoStatus.PENDING
    assert is_persistent_todo(named)
    assert is_persistent_todo(other)
    assert "Kept 2 reminder" in result["message"]


@pytest.mark.asyncio
async def test_named_delete_leaves_the_other_reminder() -> None:
    named = _persistent("tomorrow", "明早提醒")
    other = _persistent("last_week", "上周提醒")
    held = [named, other]
    tool = _modify_tool(held)
    session = _State()
    remember_turn_user_query(session, "把明早提醒这个待办删了")

    result = await tool.invoke(
        {"action": "delete", "ids": ["tomorrow", "last_week"]},
        session=session,
    )

    assert [todo.id for todo in held] == ["last_week"]
    assert is_persistent_todo(held[0])
    assert "Kept 1 reminder" in result["message"]


@pytest.mark.asyncio
async def test_create_on_a_delete_turn_still_restores_reminders() -> None:
    named = _persistent("tomorrow", "明早提醒")
    other = _persistent("last_week", "上周提醒")
    created = TodoItem(id="friday", content="周五开会", status=TodoStatus.PENDING)
    tool = _TodoFile([created])

    await restore_persistent_reminders(tool, "sess-1", [named, other])

    assert [todo.id for todo in tool.todos] == ["friday", "tomorrow", "last_week"]
    assert is_persistent_todo(tool.todos[1])
    assert is_persistent_todo(tool.todos[2])


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
