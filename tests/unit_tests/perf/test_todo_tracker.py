# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.perf.collector import get_perf_collector
from jiuwenswarm.perf.config import init_perf_summary_config
from jiuwenswarm.perf.context import clear_request_context, set_request_context
from jiuwenswarm.perf.todo_tracker import (
    clear_todo_tracker,
    sync_main_agent_todos,
    todos_from_items,
)


def _reset(request_id: str = "req-todo") -> None:
    init_perf_summary_config()
    clear_todo_tracker(request_id)
    clear_request_context(request_id=request_id)
    set_request_context(
        session_id="sess-main",
        request_id=request_id,
        channel_id="web",
        mode="agent",
    )


def test_sync_records_completed_main_todos_with_duration() -> None:
    _reset()

    sync_main_agent_todos(
        [
            {"id": "research", "content": "调研报告", "status": "in_progress"},
            {"id": "build", "content": "生成PPTX", "status": "pending"},
        ],
        request_id="req-todo",
        now=1000.0,
    )
    sync_main_agent_todos(
        [
            {"id": "research", "content": "调研报告", "status": "completed"},
            {"id": "build", "content": "生成PPTX", "status": "in_progress"},
        ],
        request_id="req-todo",
        now=1010.0,
    )
    sync_main_agent_todos(
        [
            {"id": "research", "content": "调研报告", "status": "completed"},
            {"id": "build", "content": "生成PPTX", "status": "completed"},
        ],
        request_id="req-todo",
        now=1025.0,
    )

    acc = get_perf_collector().get_accumulator("req-todo")
    assert acc is not None
    assert acc.task_count == 2
    assert [t["task_content"] for t in acc.tasks] == ["调研报告", "生成PPTX"]
    assert acc.tasks[0]["task_id"] == "todo:research"
    assert acc.tasks[0]["duration_s"] == 10.0
    assert acc.tasks[1]["duration_s"] == 15.0
    assert acc.tasks[0]["source"] == "todo"


def test_sync_is_idempotent_and_handles_pending_to_completed() -> None:
    _reset("req-todo-2")

    sync_main_agent_todos(
        [{"id": "a", "content": "一步完成", "status": "pending"}],
        request_id="req-todo-2",
        now=50.0,
    )
    sync_main_agent_todos(
        [{"id": "a", "content": "一步完成", "status": "completed"}],
        request_id="req-todo-2",
        now=55.0,
    )
    # duplicate terminal snapshot must not double-count
    sync_main_agent_todos(
        [{"id": "a", "content": "一步完成", "status": "completed"}],
        request_id="req-todo-2",
        now=60.0,
    )

    acc = get_perf_collector().get_accumulator("req-todo-2")
    assert acc is not None
    assert acc.task_count == 1
    assert acc.tasks[0]["duration_s"] == 5.0


def test_todos_from_items_normalizes_enum_like_status() -> None:
    class _Status:
        value = "IN_PROGRESS"

    class _Item:
        id = "x"
        content = "c"
        status = _Status()

    assert todos_from_items([_Item()]) == [
        {"id": "x", "content": "c", "status": "in_progress"}
    ]


def test_prune_stale_todo_trackers() -> None:
    from jiuwenswarm.perf import todo_tracker as tracker

    _reset("req-stale")
    sync_main_agent_todos(
        [{"id": "a", "content": "x", "status": "pending"}],
        request_id="req-stale",
        now=1.0,
    )
    assert tracker.prune_stale_todo_trackers(now=1.0 + tracker._STALE_AGE_S + 1) == 1
    # After prune, completing should treat as first sight (duration ~0).
    sync_main_agent_todos(
        [{"id": "a", "content": "x", "status": "completed"}],
        request_id="req-stale",
        now=9000.0,
    )
    acc = get_perf_collector().get_accumulator("req-stale")
    assert acc is not None
    assert acc.task_count == 1
    assert acc.tasks[0]["duration_s"] == 0.0


def test_in_progress_todo_binds_active_task_for_llm_tool_stats() -> None:
    from jiuwenswarm.perf.context import get_request_context, resolve_task_id
    from jiuwenswarm.perf.events import LlmPerfEvent, ToolPerfEvent

    _reset("req-bind")
    sync_main_agent_todos(
        [
            {"id": "research", "content": "调研", "status": "in_progress"},
            {"id": "build", "content": "生成", "status": "pending"},
        ],
        request_id="req-bind",
        session_id="sess-main",
        now=100.0,
    )
    req_ctx = get_request_context(session_id="sess-main")
    assert resolve_task_id(request_ctx=req_ctx) == "todo:research"

    collector = get_perf_collector()
    collector.record_llm(
        "req-bind",
        LlmPerfEvent(
            llm_call_id="llm-1",
            duration_ms=1500.0,
            model="m",
            iteration=1,
            input_tokens=10,
            output_tokens=5,
            status="ok",
            task_id=resolve_task_id(request_ctx=req_ctx),
        ),
    )
    collector.record_tool(
        "req-bind",
        ToolPerfEvent(
            tool_call_id="tool-1",
            name="bash",
            duration_ms=2000.0,
            status="ok",
            task_id=resolve_task_id(request_ctx=req_ctx),
        ),
    )

    sync_main_agent_todos(
        [
            {"id": "research", "content": "调研", "status": "completed"},
            {"id": "build", "content": "生成", "status": "in_progress"},
        ],
        request_id="req-bind",
        session_id="sess-main",
        now=110.0,
    )
    req_ctx = get_request_context(session_id="sess-main")
    assert resolve_task_id(request_ctx=req_ctx) == "todo:build"

    acc = collector.get_accumulator("req-bind")
    assert acc is not None
    assert acc.task_count == 1
    task = acc.tasks[0]
    assert task["task_id"] == "todo:research"
    assert task["stats"]["llm"]["count"] == 1
    assert task["stats"]["llm"]["total_s"] == 1.5
    assert task["stats"]["tool"]["count"] == 1
    assert task["stats"]["tool"]["total_s"] == 2.0
