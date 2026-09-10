# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard: todo.updated side-channel must filter stale todo ids.

When a fresh (non-resume) turn follows an interrupted task, the prepare hook
cancels the leftover todos and TaskExecutionRail.before_invoke captures their
ids (``set_stale_todo_ids`` on the runtime session). The ``task.update``
channel already filtered them via ``_stale_todo_ids``; this test pins the
**second** channel: ``StreamEventRail._emit_todo_updated`` pushes the whole
todo.json snapshot after every todo tool call (e.g. the LLM's own
``todo_modify`` cancelling old tasks) — without the session-state filter, the
old tasks' completed rows re-pop the frontend todo panel ("中断恢复后 todo
任务又跳出来").

Observed live (session officeclaw_569f53f14985c97acfc126a6): the LLM's
todo_modify executed successfully → after_tool_call emitted todo.updated with
the full 6-task list (3 completed + 3 just-cancelled) → relay converted it to
a task_progress snapshot → frontend accepted it (non-empty) → stale tasks
popped. Runs where todo_modify never completed (parallel web_search won the
race) did not pop — the intermittent symptom.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    STALE_TODO_IDS_SESSION_KEY,
    get_stale_todo_ids,
    set_stale_todo_ids,
    clear_stale_todo_ids,
)


class _FakeState:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def update_global(self, data: dict[str, Any]) -> None:
        self._store.update(data)

    def get_global(self, key: Any = None) -> Any:
        if key is None:
            return dict(self._store)
        return self._store.get(key) if isinstance(key, str) else None


class _FakeSession:
    def __init__(self) -> None:
        self._state = _FakeState()

    def update_state(self, data: dict[str, Any]) -> None:
        self._state.update_global(data)

    def get_state(self, key: Any = None) -> Any:
        return self._state.get_global(key)


class _FakeTodoItem(SimpleNamespace):
    pass


def _todos(*specs: tuple[str, str]) -> list[_FakeTodoItem]:
    from openjiuwen.harness.schema.task import TodoStatus

    status_enum = {
        "pending": TodoStatus.PENDING,
        "in_progress": TodoStatus.IN_PROGRESS,
        "completed": TodoStatus.COMPLETED,
        "cancelled": TodoStatus.CANCELLED,
    }
    return [
        _FakeTodoItem(
            id=tid,
            content=f"task {tid}",
            activeForm=f"task {tid}",
            status=status_enum[status],
        )
        for tid, status in specs
    ]


def test_set_get_clear_roundtrip() -> None:
    session = _FakeSession()
    assert get_stale_todo_ids(session) == set()

    set_stale_todo_ids(session, {"b", "a", "c"})
    # Stored as a sorted JSON-able list.
    assert session.get_state(STALE_TODO_IDS_SESSION_KEY) == ["a", "b", "c"]
    assert get_stale_todo_ids(session) == {"a", "b", "c"}

    clear_stale_todo_ids(session)
    assert get_stale_todo_ids(session) == set()
    assert session.get_state(STALE_TODO_IDS_SESSION_KEY) is None


def test_get_stale_todo_ids_tolerates_garbage() -> None:
    session = _FakeSession()
    session.update_state({STALE_TODO_IDS_SESSION_KEY: "not-a-list"})
    assert get_stale_todo_ids(session) == set()
    session.update_state({STALE_TODO_IDS_SESSION_KEY: [1, "", None, "x"]})
    assert get_stale_todo_ids(session) == {"1", "x"}


@pytest.mark.asyncio
async def test_emit_todo_updated_no_filter_without_stale_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a stale set, todo.updated passes the full (already cancelled-stripped) list."""
    from jiuwenswarm.agents.harness.common.rails import stream_event_rail

    rail = object.__new__(stream_event_rail.JiuSwarmStreamEventRail)
    rail._member_name = ""  # pylint: disable=protected-access
    rail._main_todo_tool = None  # pylint: disable=protected-access
    disk_todos = _todos(("a", "completed"), ("b", "in_progress"))

    class _FakeTodoTool:
        async def load_todos(self, _session_id: str) -> list[_FakeTodoItem]:
            return list(disk_todos)

    monkeypatch.setattr(rail, "_get_todo_tool", lambda: _FakeTodoTool())

    pushed: list[dict[str, Any]] = []

    class _FakeSessionWithStream(_FakeSession):
        async def write_stream(self, schema: Any) -> None:
            pushed.append(schema.payload)

    session = _FakeSessionWithStream()

    await rail._emit_todo_updated(session, "sess-1")  # pylint: disable=protected-access

    assert len(pushed) == 1
    assert [t["id"] for t in pushed[0]["todos"]] == ["a", "b"]
