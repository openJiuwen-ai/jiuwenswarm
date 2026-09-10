# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""todo_create must not emit task.start; work tools lazily open the segment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY,
    TaskExecutionRail,
    get_current_task_id,
)


class _FakeSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    def get_session_id(self) -> str:
        return "sess-todo-defer"

    async def write_stream(self, schema: object) -> None:
        self.events.append(schema)


def _event_types(session: _FakeSession) -> list[str | None]:
    return [getattr(ev, "type", None) for ev in session.events]


@pytest.mark.asyncio
async def test_todo_create_defers_task_start_for_in_progress(monkeypatch) -> None:
    rail = TaskExecutionRail()
    session = _FakeSession()
    rail._todo_map_before_tool = {}
    after_items = [
        {
            "id": "sync_weekly_report_0831",
            "content": "下周一同步周报",
            "status": "in_progress",
        },
    ]
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_items)

    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name="todo_create", request_id="req-create"),
    )
    await rail._sync_todo_and_emit_transitions(ctx)

    assert _event_types(session) == ["task.update"]
    assert "sync_weekly_report_0831" not in rail._todo_started
    assert "todo:sync_weekly_report_0831" not in rail._active_tasks
    assert get_current_task_id() == "todo:sync_weekly_report_0831"


@pytest.mark.asyncio
async def test_todo_modify_still_emits_task_start(monkeypatch) -> None:
    rail = TaskExecutionRail()
    session = _FakeSession()
    rail._todo_map_before_tool = {
        "sync_weekly_report_0831": {
            "content": "下周一同步周报",
            "status": "pending",
            "index": 0,
            "total": 1,
        },
    }
    after_items = [
        {
            "id": "sync_weekly_report_0831",
            "content": "下周一同步周报",
            "status": "in_progress",
        },
    ]
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_items)

    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name="todo_modify", request_id="req-modify"),
    )
    await rail._sync_todo_and_emit_transitions(ctx)

    assert _event_types(session)[:1] == ["task.start"]
    assert "sync_weekly_report_0831" in rail._todo_started


@pytest.mark.asyncio
async def test_lazy_start_opens_deferred_in_progress_on_work_tool(monkeypatch) -> None:
    rail = TaskExecutionRail()
    session = _FakeSession()
    rail._todo_map = {
        "sync_weekly_report_0831": {
            "content": "下周一同步周报",
            "status": "in_progress",
            "index": 0,
            "total": 1,
        },
    }
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: [
        {
            "id": "sync_weekly_report_0831",
            "content": "下周一同步周报",
            "status": "in_progress",
        },
    ])

    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name="bash", request_id="req-work"),
    )
    await rail._lazy_start_in_progress_todo_on_work_tool(ctx)

    assert _event_types(session) == ["task.start", "task.update"]
    assert "sync_weekly_report_0831" in rail._todo_started
    assert "todo:sync_weekly_report_0831" in rail._active_tasks


@pytest.mark.asyncio
async def test_skill_turbo_tool_call_exports_outer_todo_display_ownership() -> None:
    rail = TaskExecutionRail()
    rail._todo_map = {
        "create_ppt": {
            "content": "生成 PPT",
            "status": "pending",
            "index": 0,
            "total": 1,
        },
    }
    tool_call = SimpleNamespace(
        id="call-skill-turbo",
        name="skill_acceleration_exec",
        arguments={"query": "生成 PPT"},
    )
    ctx = SimpleNamespace(
        session=_FakeSession(),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_call.name,
            tool_args=tool_call.arguments,
            tool_result=None,
        ),
        extra={},
    )

    await rail.before_tool_call(ctx)

    assert get_current_task_id() == "todo:create_ppt"
    assert ctx.extra[SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY] is True


@pytest.mark.asyncio
async def test_deferred_in_progress_completed_emits_start_then_complete(monkeypatch) -> None:
    rail = TaskExecutionRail()
    session = _FakeSession()
    rail._todo_map_before_tool = {
        "sync_weekly_report_0831": {
            "content": "下周一同步周报",
            "status": "in_progress",
            "index": 0,
            "total": 1,
        },
    }
    after_items = [
        {
            "id": "sync_weekly_report_0831",
            "content": "下周一同步周报",
            "status": "completed",
        },
    ]
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_items)

    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(tool_name="todo_modify", request_id="req-done"),
    )
    await rail._sync_todo_and_emit_transitions(ctx)

    assert _event_types(session)[:2] == ["task.start", "task.complete"]
