# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parallel work tools bind matched todos; list status stays serial."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TaskExecutionRail,
    get_current_task_id,
)


class _FakeSession:
    def __init__(self, session_id: str = "sess-parallel") -> None:
        self.events: list[object] = []
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id

    async def write_stream(self, schema: object) -> None:
        self.events.append(schema)


def _event_types(session: _FakeSession) -> list[str | None]:
    return [getattr(ev, "type", None) for ev in session.events]


def _todo_item(task_id: str, content: str, status: str, index: int, total: int = 4) -> dict:
    return {
        "content": content,
        "activeForm": f"正在{content}",
        "status": status,
        "index": index,
        "total": total,
    }


def _write_todos(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def _work_ctx(session: _FakeSession, *, name: str, arguments: dict, display_name: str = ""):
    tool_call = SimpleNamespace(
        id=f"call-{name}",
        name=name,
        arguments=arguments,
        display_name=display_name,
    )
    return SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=name,
            tool_args=arguments,
            tool_result=None,
        ),
        extra={},
    )


@pytest.fixture
def todo_file(tmp_path: Path) -> Path:
    return tmp_path / "sess-parallel" / "todo.json"


def _rail_with_hangzhou_todos(todo_file: Path) -> TaskExecutionRail:
    items = [
        {
            "id": "search_temp",
            "content": "搜索杭州最近一个月气温数据",
            "activeForm": "正在搜索杭州气温数据",
            "status": "in_progress",
        },
        {
            "id": "load_skill",
            "content": "加载 xlsx-craft 技能",
            "activeForm": "正在加载 xlsx-craft 技能",
            "status": "pending",
        },
        {
            "id": "create_excel",
            "content": "生成含图表的 Excel 文件",
            "activeForm": "正在生成 Excel 文件",
            "status": "pending",
        },
        {
            "id": "deliver",
            "content": "将文件发送给用户",
            "activeForm": "正在发送文件",
            "status": "pending",
        },
    ]
    _write_todos(todo_file, items)
    rail = TaskExecutionRail()
    rail._todo_map = rail._build_map_from_todo_items(items)
    rail._get_todo_workspace_path = lambda _sid: todo_file  # type: ignore[method-assign]
    return rail


@pytest.mark.asyncio
async def test_skill_tool_does_not_open_later_todo_while_search_runs(
    todo_file: Path,
) -> None:
    rail = _rail_with_hangzhou_todos(todo_file)
    session = _FakeSession()
    ctx = _work_ctx(
        session,
        name="skill_tool",
        arguments={"skill_name": "xlsx-craft"},
        display_name="加载 xlsx-craft 技能以生成 Excel 文件",
    )

    await rail.after_tool_call(ctx)

    disk = json.loads(todo_file.read_text(encoding="utf-8"))
    by_id = {item["id"]: item["status"] for item in disk}
    assert by_id["search_temp"] == "in_progress"
    assert by_id["load_skill"] == "pending"
    assert by_id["create_excel"] == "pending"
    assert "load_skill" not in rail._todo_started
    assert "task.start" not in _event_types(session)
    assert get_current_task_id() == "todo:load_skill"


@pytest.mark.asyncio
async def test_web_search_still_lazy_starts_first_in_progress_todo(todo_file: Path) -> None:
    rail = _rail_with_hangzhou_todos(todo_file)
    session = _FakeSession()
    ctx = _work_ctx(
        session,
        name="web_search",
        arguments={"query": "杭州 2026年8月 气温"},
        display_name="搜索杭州最近一个月气温数据",
    )

    await rail.after_tool_call(ctx)

    disk = json.loads(todo_file.read_text(encoding="utf-8"))
    by_id = {item["id"]: item["status"] for item in disk}
    assert by_id["search_temp"] == "in_progress"
    assert by_id["load_skill"] == "pending"
    assert "search_temp" in rail._todo_started
    assert get_current_task_id() == "todo:search_temp"


@pytest.mark.asyncio
async def test_unmatched_bash_does_not_advance_later_pending_todo(todo_file: Path) -> None:
    rail = _rail_with_hangzhou_todos(todo_file)
    session = _FakeSession()
    ctx = _work_ctx(
        session,
        name="bash",
        arguments={"command": "ls"},
    )

    await rail.after_tool_call(ctx)

    disk = json.loads(todo_file.read_text(encoding="utf-8"))
    by_id = {item["id"]: item["status"] for item in disk}
    assert by_id["load_skill"] == "pending"
    assert by_id["create_excel"] == "pending"
    assert "search_temp" in rail._todo_started
    assert "load_skill" not in rail._todo_started


def test_task_update_uses_list_index_when_json_has_no_index_field() -> None:
    rail = TaskExecutionRail()
    items = [
        {"id": "search_temp", "content": "搜索", "status": "in_progress"},
        {"id": "load_skill", "content": "加载技能", "status": "pending"},
    ]
    rail._todo_map = rail._build_map_from_todo_items(items)
    formatted = rail._format_tasks_for_update(items, source="todo")
    assert [row["task_index"] for row in formatted] == [0, 1]


def test_resolve_prefers_skill_name_over_first_in_progress() -> None:
    rail = TaskExecutionRail()
    rail._todo_map = {
        "search_temp": _todo_item("search_temp", "搜索杭州最近一个月气温数据", "in_progress", 0),
        "load_skill": _todo_item("load_skill", "加载 xlsx-craft 技能", "pending", 1),
    }
    ctx = _work_ctx(
        _FakeSession(),
        name="skill_tool",
        arguments={"skill_name": "xlsx-craft"},
    )
    assert rail._resolve_work_tool_todo_id(ctx) == "load_skill"


def test_overlay_hides_later_completed_while_earlier_in_progress() -> None:
    rail = TaskExecutionRail()
    items = [
        {"id": "search_temp", "content": "搜索", "status": "in_progress"},
        {"id": "load_skill", "content": "加载技能", "status": "completed"},
    ]
    overlay = rail._overlay_serial_todo_statuses(items)
    assert [row["status"] for row in overlay] == ["in_progress", "pending"]


@pytest.mark.asyncio
async def test_later_complete_is_deferred_until_earlier_completes(monkeypatch) -> None:
    rail = TaskExecutionRail()
    session = _FakeSession()
    rail._todo_map_before_tool = {
        "search_temp": _todo_item("search_temp", "搜索", "in_progress", 0),
        "load_skill": _todo_item("load_skill", "加载技能", "pending", 1),
    }
    rail._todo_started.add("search_temp")
    after_first = [
        {"id": "search_temp", "content": "搜索", "status": "in_progress"},
        {"id": "load_skill", "content": "加载技能", "status": "completed"},
    ]
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_first)

    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name="todo_modify", request_id="req-1"),
    )
    await rail._sync_todo_and_emit_transitions(ctx)

    types = _event_types(session)
    assert "task.start" not in types
    assert "task.complete" not in types
    assert "load_skill" in rail._todo_complete_deferred
    update = [ev for ev in session.events if getattr(ev, "type", None) == "task.update"][-1]
    statuses = {
        row["task_id"]: row["status"]
        for row in getattr(update, "payload", {}).get("tasks", [])
    }
    assert statuses["search_temp"] == "in_progress"
    assert statuses["load_skill"] == "pending"

    session.events.clear()
    after_search_done = [
        {"id": "search_temp", "content": "搜索", "status": "completed"},
        {"id": "load_skill", "content": "加载技能", "status": "completed"},
    ]
    rail._todo_map_before_tool = rail._todo_map
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_search_done)
    await rail._sync_todo_and_emit_transitions(ctx)

    types = _event_types(session)
    assert types[:4] == [
        "task.complete",
        "task.start",
        "task.complete",
        "task.update",
    ]
    payloads = [getattr(ev, "payload", {}) for ev in session.events]
    assert payloads[0]["task_id"] == "todo:search_temp"
    assert payloads[1]["task_id"] == "todo:load_skill"
    assert payloads[2]["task_id"] == "todo:load_skill"
    assert "load_skill" not in rail._todo_complete_deferred
    update = [ev for ev in session.events if getattr(ev, "type", None) == "task.update"][-1]
    statuses = {
        row["task_id"]: row["status"]
        for row in getattr(update, "payload", {}).get("tasks", [])
    }
    assert statuses["search_temp"] == "completed"
    assert statuses["load_skill"] == "completed"


def test_rebuild_serial_deferred_from_disk_snapshot() -> None:
    rail = TaskExecutionRail()
    rail._todo_map = {
        "search_temp": _todo_item("search_temp", "搜索", "in_progress", 0),
        "load_skill": _todo_item("load_skill", "加载技能", "completed", 1),
        "create_excel": _todo_item("create_excel", "生成 Excel", "in_progress", 2),
    }
    rail._todo_complete_deferred = {"stale"}
    rail._todo_start_deferred = {"stale"}
    rail._rebuild_serial_todo_deferred()
    assert rail._todo_complete_deferred == {"load_skill"}
    assert rail._todo_start_deferred == {"create_excel"}


@pytest.mark.asyncio
async def test_rebuilt_deferred_flush_after_new_invoke(monkeypatch) -> None:
    """before_invoke clears in-memory deferred; rebuild from disk then flush."""
    rail = TaskExecutionRail()
    session = _FakeSession()
    on_disk = [
        {"id": "search_temp", "content": "搜索", "status": "in_progress"},
        {"id": "load_skill", "content": "加载技能", "status": "completed"},
    ]
    rail._todo_map = rail._build_map_from_todo_items(on_disk)
    rail._todo_started.add("search_temp")
    rail._rebuild_serial_todo_deferred()
    assert "load_skill" in rail._todo_complete_deferred

    after_search_done = [
        {"id": "search_temp", "content": "搜索", "status": "completed"},
        {"id": "load_skill", "content": "加载技能", "status": "completed"},
    ]
    rail._todo_map_before_tool = dict(rail._todo_map)
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_search_done)
    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name="todo_modify", request_id="req-2"),
    )
    await rail._sync_todo_and_emit_transitions(ctx)

    types = _event_types(session)
    assert types[:4] == [
        "task.complete",
        "task.start",
        "task.complete",
        "task.update",
    ]
    payloads = [getattr(ev, "payload", {}) for ev in session.events]
    assert payloads[0]["task_id"] == "todo:search_temp"
    assert payloads[1]["task_id"] == "todo:load_skill"
    assert payloads[2]["task_id"] == "todo:load_skill"

