# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TaskExecutionRail,
)


class _FakeSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    def get_session_id(self) -> str:
        return "sess-1"

    async def write_stream(self, schema: object) -> None:
        self.events.append(schema)


def _task(content: str, status: str, index: int, total: int = 3) -> dict:
    return {
        "content": content,
        "status": status,
        "index": index,
        "total": total,
    }


@pytest.mark.asyncio
async def test_pending_to_completed_emits_start_then_complete(monkeypatch) -> None:
    rail = TaskExecutionRail()
    session = _FakeSession()
    rail._todo_map_before_tool = {
        "stage2_outline": _task("Stage 2-3", "in_progress", 1),
        "stage4_research": _task("Stage 4: 深度研究", "pending", 2),
        "stage5_style": _task("Stage 5: 风格", "pending", 3),
        "stage6_generate": _task("Stage 6: 生成", "pending", 4),
    }
    after_items = [
        {"id": "stage2_outline", "content": "Stage 2-3", "status": "completed"},
        {"id": "stage4_research", "content": "Stage 4: 深度研究", "status": "completed"},
        {"id": "stage5_style", "content": "Stage 5: 风格", "status": "completed"},
        {"id": "stage6_generate", "content": "Stage 6: 生成", "status": "in_progress"},
    ]
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: after_items)
    rail._todo_started.add("stage2_outline")
    rail._active_tasks["todo:stage2_outline"] = SimpleNamespace(
        task_id="todo:stage2_outline",
        task_content="Stage 2-3",
        source="todo",
        start_time=0.0,
    )

    ctx = SimpleNamespace(
        session=session,
        inputs=SimpleNamespace(request_id="req-1"),
    )
    await rail._sync_todo_and_emit_transitions(ctx)

    types = [getattr(ev, "type", None) for ev in session.events]
    assert types[:6] == [
        "task.complete",
        "task.start",
        "task.complete",
        "task.start",
        "task.complete",
        "task.start",
    ]
    assert types[-1] == "task.update"

    payloads = [getattr(ev, "payload", {}) for ev in session.events]
    assert payloads[0]["task_id"] == "todo:stage2_outline"
    assert payloads[1]["task_id"] == "todo:stage4_research"
    assert payloads[2]["task_id"] == "todo:stage4_research"
    assert payloads[3]["task_id"] == "todo:stage5_style"
    assert payloads[4]["task_id"] == "todo:stage5_style"
    assert payloads[5]["task_id"] == "todo:stage6_generate"
    assert "stage4_research" in rail._todo_started
    assert "stage5_style" in rail._todo_started
    assert "stage6_generate" in rail._todo_started
