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

    def get_state(self, _key: str) -> object:
        return None

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


@pytest.mark.asyncio
async def test_ppt_turbo_fail_skips_task_start_so_bubble_text_keeps_updating(
    monkeypatch,
) -> None:
    """加速失败降级后不再发 todo task.start，避免后续 [当前步骤] 被任务栈吞掉。"""
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        ppt_turbo_keep_bubble_text,
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(True)
    try:
        assert ppt_turbo_keep_bubble_text() is True
        rail = TaskExecutionRail()
        session = _FakeSession()
        rail._todo_map = {
            "env_check": {
                "content": "环境检测与已有产物盘点",
                "status": "in_progress",
                "index": 0,
                "total": 2,
            },
        }
        monkeypatch.setattr(
            rail,
            "_load_todo_from_json",
            lambda _sid: [
                {
                    "id": "env_check",
                    "content": "环境检测与已有产物盘点",
                    "status": "in_progress",
                },
            ],
        )
        ctx = SimpleNamespace(
            session=session,
            inputs=SimpleNamespace(tool_name="bash", request_id="req-work"),
        )
        await rail._lazy_start_in_progress_todo_on_work_tool(ctx)

        types = _event_types(session)
        assert "task.start" not in types
        assert "task.update" in types
        assert "llm_output" in types
        step_text = [
            getattr(ev, "payload", {}).get("content", "")
            for ev in session.events
            if getattr(ev, "type", None) == "llm_output"
        ]
        assert any("当前步骤" in text and "环境检测" in text for text in step_text)
        assert "env_check" in rail._todo_started
        assert "todo:env_check" in rail._active_tasks
    finally:
        set_ppt_turbo_keep_bubble_text(False)


@pytest.mark.asyncio
async def test_ppt_turbo_fail_todo_advance_writes_next_bubble_step(
    monkeypatch,
) -> None:
    """降级后 todo 切到下一步时，左边正文跟上右侧当前项。"""
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(True)
    try:
        rail = TaskExecutionRail()
        session = _FakeSession()
        rail._todo_map_before_tool = {
            "env_check": {
                "content": "检查环境与已有产物",
                "status": "in_progress",
                "index": 0,
                "total": 2,
            },
            "html_generate": {
                "content": "生成 HTML 页面",
                "status": "pending",
                "index": 1,
                "total": 2,
            },
        }
        rail._todo_started.add("env_check")
        monkeypatch.setattr(
            rail,
            "_load_todo_from_json",
            lambda _sid: [
                {
                    "id": "env_check",
                    "content": "检查环境与已有产物",
                    "status": "completed",
                },
                {
                    "id": "html_generate",
                    "content": "生成 HTML 页面",
                    "status": "in_progress",
                },
            ],
        )
        ctx = SimpleNamespace(
            session=session,
            inputs=SimpleNamespace(tool_name="todo_modify", request_id="req-html"),
        )
        await rail._sync_todo_and_emit_transitions(ctx)

        types = _event_types(session)
        assert "task.start" not in types
        assert "task.complete" not in types
        assert "task.update" in types
        step_text = [
            getattr(ev, "payload", {}).get("content", "")
            for ev in session.events
            if getattr(ev, "type", None) == "llm_output"
        ]
        assert any("生成 HTML 页面" in text for text in step_text)
    finally:
        set_ppt_turbo_keep_bubble_text(False)


def test_ppt_turbo_keep_bubble_survives_copied_context() -> None:
    """gather 会 copy_context；标记必须是模块级，不能困在拷贝里。"""
    import contextvars

    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        ppt_turbo_keep_bubble_text,
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(False)
    copied = contextvars.copy_context()

    def _set_in_copy() -> None:
        set_ppt_turbo_keep_bubble_text(True)

    copied.run(_set_in_copy)
    try:
        assert ppt_turbo_keep_bubble_text() is True
    finally:
        set_ppt_turbo_keep_bubble_text(False)


@pytest.mark.asyncio
async def test_fresh_invoke_clears_ppt_turbo_keep_bubble_flag() -> None:
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        ppt_turbo_keep_bubble_text,
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(True, session_id="sess-todo-defer")
    rail = TaskExecutionRail()
    ctx = SimpleNamespace(
        session=_FakeSession(),
        inputs=SimpleNamespace(query="帮我再生成一份新的周报PPT"),
    )
    await rail.before_invoke(ctx)
    assert ppt_turbo_keep_bubble_text() is False


@pytest.mark.asyncio
async def test_permission_resume_keeps_ppt_turbo_bubble_flag() -> None:
    """权限卡「本次允许」是新 invoke，但不能清 keep-bubble 标记。"""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        ppt_turbo_keep_bubble_text,
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(True, session_id="sess-todo-defer")
    try:
        rail = TaskExecutionRail()
        ctx = SimpleNamespace(
            session=_FakeSession(),
            inputs=SimpleNamespace(query=InteractiveInput()),
        )
        await rail.before_invoke(ctx)
        assert ppt_turbo_keep_bubble_text("sess-todo-defer") is True
    finally:
        set_ppt_turbo_keep_bubble_text(False)


@pytest.mark.asyncio
async def test_empty_query_resume_keeps_ppt_turbo_bubble_flag() -> None:
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        ppt_turbo_keep_bubble_text,
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(True, session_id="sess-todo-defer")
    try:
        rail = TaskExecutionRail()
        ctx = SimpleNamespace(
            session=_FakeSession(),
            inputs=SimpleNamespace(query=""),
        )
        await rail.before_invoke(ctx)
        assert ppt_turbo_keep_bubble_text("sess-todo-defer") is True
    finally:
        set_ppt_turbo_keep_bubble_text(False)


@pytest.mark.asyncio
async def test_subagent_before_invoke_keeps_ppt_turbo_bubble_flag() -> None:
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        ppt_turbo_keep_bubble_text,
        set_ppt_turbo_keep_bubble_text,
    )

    set_ppt_turbo_keep_bubble_text(True)
    try:
        rail = TaskExecutionRail()
        ctx = SimpleNamespace(session=None, inputs=SimpleNamespace())
        await rail.before_invoke(ctx)
        assert ppt_turbo_keep_bubble_text() is True
    finally:
        set_ppt_turbo_keep_bubble_text(False)

