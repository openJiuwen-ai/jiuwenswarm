# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""todo_create must not emit task.start; work tools lazily open the segment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY,
    SKILL_TURBO_OUTER_TODO_COMPLETED_BEFORE_EXTRA_KEY,
    TaskExecutionRail,
    get_current_task_id,
)


@pytest.fixture(autouse=True)
def persisted_snapshot_boundary(tmp_path, monkeypatch):
    # Algorithm mocks now also supply the authoritative file read used by events.
    import json
    original = TaskExecutionRail._get_todo_workspace_path
    def path_for(rail, session_id):
        if "_load_todo_from_json" not in rail.__dict__:
            return original(rail, session_id)
        path = tmp_path / f"{id(rail)}.json"
        path.write_text(json.dumps(rail._load_todo_from_json(session_id)))
        return path
    monkeypatch.setattr(TaskExecutionRail, "_get_todo_workspace_path", path_for)


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


def test_completed_items_before_counts_earlier_done_items() -> None:
    """B_04 放行信号口径：早于绑定项的已完成项数（completed/cancelled 均计）。"""
    rail = TaskExecutionRail()
    rail._todo_map = {
        "ppt_libai": {"content": "李白PPT", "status": "completed", "index": 0, "total": 3},
        "ppt_dufu": {"content": "杜甫PPT", "status": "in_progress", "index": 1, "total": 3},
        "ppt_baiyi": {"content": "白居易PPT", "status": "cancelled", "index": 2, "total": 3},
        "ppt_dumu": {"content": "杜牧PPT", "status": "pending", "index": 3, "total": 3},
    }

    # 绑定第 2 项：早前 1 个 completed → 后续交付物信号
    assert rail._completed_items_before("todo:ppt_dufu") == 1
    # 绑定第 4 项：早前 completed/cancelled 各一计 2（in_progress 不是终态不计）
    assert rail._completed_items_before("ppt_dumu") == 2
    # 绑定首项：早前无已完成项 → 0（非 None，绑定存在但无前驱）
    assert rail._completed_items_before("todo:ppt_libai") == 0
    # 无绑定 / 项不在清单 → None
    assert rail._completed_items_before(None) is None
    assert rail._completed_items_before("todo:not_in_map") is None


@pytest.mark.asyncio
async def test_skill_turbo_tool_call_exports_completed_before_extra() -> None:
    """B_04 绑定导出链：skill_acceleration_exec 绑定后续项时导出 completed_before。

    多交付物清单（F_03 流程）第二项 in_progress、首项 completed：绑定分支
    必须导出 completed_before=1，供 StreamEventRail → context_binding →
    fresh 守卫放行后续交付物。
    """
    rail = TaskExecutionRail()
    rail._todo_map = {
        "ppt_libai": {
            "content": "生成李白PPT",
            "status": "completed",
            "index": 0,
            "total": 2,
        },
        "ppt_dufu": {
            "content": "生成杜甫PPT",
            "status": "in_progress",
            "index": 1,
            "total": 2,
        },
    }
    tool_call = SimpleNamespace(
        id="call-skill-turbo-2nd",
        name="skill_acceleration_exec",
        arguments={"query": "生成杜甫PPT"},
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

    # 绑定到清单推进的第二项（唯一非终态项），并导出后续交付物信号
    assert get_current_task_id() == "todo:ppt_dufu"
    assert ctx.extra[SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY] is True
    assert ctx.extra[SKILL_TURBO_OUTER_TODO_COMPLETED_BEFORE_EXTRA_KEY] == 1


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


def test_external_todo_completion_events_build_complete_and_update(monkeypatch) -> None:
    rail = TaskExecutionRail()
    todo_items = [
        {
            "id": "create_ppt",
            "content": "生成华为风格 PPT",
            "status": "completed",
        },
    ]
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: todo_items)

    events = rail.external_todo_completion_events(
        "sess-todo-defer",
        "todo:create_ppt",
        parent_request_id="resume-request",
    )

    assert [event.type for event in events] == ["task.complete", "task.update"]
    assert events[0].payload["task_id"] == "todo:create_ppt"
    assert events[0].payload["status"] == "succeeded"
    assert events[0].payload["parent_request_id"] == "resume-request"
    assert events[1].payload["completed_tasks"] == 1
    assert events[1].payload["in_progress_tasks"] == 0
    assert events[1].payload["tasks"][0]["status"] == "completed"
