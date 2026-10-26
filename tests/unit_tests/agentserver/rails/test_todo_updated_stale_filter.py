# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard: todo.updated side-channel must filter stale generations.

When a fresh (non-resume) turn follows an interrupted task, the prepare hook
bumps the todo generation token (request isolation, P1-2). The ``task.update``
channel filters old-generation entries via ``_load_todo_from_json``; this test
pins the **second** channel: ``StreamEventRail._emit_todo_updated`` pushes the
whole todo.json snapshot after every todo tool call (e.g. the LLM's own
``todo_modify`` touching old tasks) — without the generation-token filter, the
old tasks' completed rows re-pop the frontend todo panel ("中断恢复后 todo
任务又跳出来").

Observed live (session officeclaw_569f53f14985c97acfc126a6): the LLM's
todo_modify executed successfully → after_tool_call emitted todo.updated with
the full 6-task list (3 completed + 3 from the interrupted generation) → relay
converted it to a task_progress snapshot → frontend accepted it (non-empty) →
stale tasks popped. Runs where todo_modify never completed (parallel
web_search won the race) did not pop — the intermittent symptom.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.harness.tools.todo import TODO_GENERATION_TOKEN_SESSION_KEY


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


def _todos(*specs: tuple[str, str, str | None]) -> list[_FakeTodoItem]:
    """Build fake items: (id, status, generation_token)."""
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
            generation_token=token,
        )
        for tid, status, token in specs
    ]


# The session-state key is owned by openjiuwen (imported at the top) so the
# tests write the exact key the tools / rails read.


def test_token_roundtrip_and_filter() -> None:
    """bump/get roundtrip + generation filtering semantics."""
    from jiuwenswarm.agents.harness.common.tools.todo_resume import (
        bump_todo_generation_token,
        filter_todos_by_generation,
        get_todo_generation_token,
        todo_item_generation_token,
    )

    session = _FakeSession()
    assert get_todo_generation_token(session) is None

    token = bump_todo_generation_token(session)
    assert token and get_todo_generation_token(session) == token

    items = _todos(
        ("legacy", "completed", None),        # 未打标：放行（legacy 磁盘残留）
        ("current", "in_progress", token),   # 当前代：放行
        ("stale_active", "in_progress", "gen-old"),  # 旧代活跃项：过滤
        ("stale_done", "completed", "gen-old"),      # 旧代终态项：过滤
    )
    kept = filter_todos_by_generation(items, token)
    assert [t.id for t in kept] == ["legacy", "current"]

    # 无 token（fail-open）：全部放行。
    assert filter_todos_by_generation(items, None) == items

    # dict 形态（task.update 通道的磁盘快照）同样支持。
    dicts = [
        {"id": "d1", "status": "completed", "generation_token": "gen-old"},
        {"id": "d2", "status": "in_progress", "generation_token": token},
        {"id": "d3", "status": "pending", "generation_token": None},
    ]
    assert [d["id"] for d in filter_todos_by_generation(dicts, token)] == ["d2", "d3"]
    assert todo_item_generation_token(dicts[0]) == "gen-old"


@pytest.mark.asyncio
async def test_emit_todo_updated_filters_stale_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """todo.updated must drop entries stamped with a superseded token."""
    from jiuwenswarm.agents.harness.common.rails import stream_event_rail

    rail = object.__new__(stream_event_rail.JiuSwarmStreamEventRail)
    rail._member_name = ""  # pylint: disable=protected-access
    rail._main_todo_tool = None  # pylint: disable=protected-access
    disk_todos = _todos(
        ("old_completed", "completed", "gen-old"),
        ("old_active", "in_progress", "gen-old"),
        ("new_active", "in_progress", "gen-current"),
        ("legacy_unstamped", "completed", None),
    )

    class _FakeTodoTool:
        async def load_todos(self, _session_id: str) -> list[_FakeTodoItem]:
            return list(disk_todos)

    monkeypatch.setattr(rail, "_get_todo_tool", lambda: _FakeTodoTool())

    pushed: list[dict[str, Any]] = []

    class _FakeSessionWithStream(_FakeSession):
        async def write_stream(self, schema: Any) -> None:
            pushed.append(schema.payload)

    session = _FakeSessionWithStream()
    session.update_state({TODO_GENERATION_TOKEN_SESSION_KEY: "gen-current"})

    await rail._emit_todo_updated(session, "sess-1")  # pylint: disable=protected-access

    assert len(pushed) == 1
    assert [t["id"] for t in pushed[0]["todos"]] == [
        "new_active",
        "legacy_unstamped",
    ]


@pytest.mark.asyncio
async def test_emit_todo_updated_no_filter_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a token on the session, todo.updated passes the full list (fail-open)."""
    from jiuwenswarm.agents.harness.common.rails import stream_event_rail

    rail = object.__new__(stream_event_rail.JiuSwarmStreamEventRail)
    rail._member_name = ""  # pylint: disable=protected-access
    rail._main_todo_tool = None  # pylint: disable=protected-access
    disk_todos = _todos(("a", "completed", None), ("b", "in_progress", None))

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


@pytest.mark.asyncio
async def test_emit_todo_updated_overlays_later_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """todo.updated must use the same serial overlay as task.update."""
    from jiuwenswarm.agents.harness.common.rails import stream_event_rail

    rail = object.__new__(stream_event_rail.JiuSwarmStreamEventRail)
    rail._member_name = ""  # pylint: disable=protected-access
    rail._main_todo_tool = None  # pylint: disable=protected-access
    disk_todos = _todos(
        ("search_temp", "in_progress", None),
        ("load_skill", "completed", None),
    )

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
    by_id = {row["id"]: row["status"] for row in pushed[0]["todos"]}
    assert by_id["search_temp"] == "in_progress"
    assert by_id["load_skill"] == "pending"
