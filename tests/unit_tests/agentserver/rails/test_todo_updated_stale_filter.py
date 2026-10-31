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

import json
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
@pytest.mark.parametrize("source", ["committed", "loaded"])
@pytest.mark.parametrize("reminder_generation", ["old", "current"])
@pytest.mark.parametrize(
    "mode,tool_name,arguments,expected_ids,expected_status",
    [
        ("legacy", "todo_modify", {"todos": [{"id": "A"}]}, ["A"], ["in_progress"]),
        ("legacy", "todo_list", {}, ["R", "A"], ["pending", "pending"]),
        ("legacy", "todo", {"action": "list"}, ["R", "A"], ["pending", "pending"]),
        ("legacy", "todo", {"action": "modify", "todos": [{"id": "A"}]}, ["A"], ["in_progress"]),
        ("legacy", "todo", {"action": "modify", "todos": [{"id": "R"}]}, ["R", "A"], ["pending", "pending"]),
        ("versioned", "todo_modify", {"todos": [{"id": "A"}]}, ["R", "A", "old"],
         ["pending", "in_progress", "completed"]),
    ],
)
async def test_committed_todo_updated_respects_consumer_and_tool_intent(
    monkeypatch, source, reminder_generation, mode, tool_name, arguments, expected_ids, expected_status,
):
    from openjiuwen.harness.tools.todo_store import decode_snapshot

    from jiuwenswarm.agents.harness.common.rails import stream_event_rail
    from jiuwenswarm.common.todo_snapshot import TODO_SNAPSHOT_MODE

    snapshot = {"plan_id": "P", "revision": 8, "todos": [
        {"id": "R", "content": "reminder", "status": "pending", "generation_token": reminder_generation,
         "meta_data": {"persistent": True}},
        {"id": "A", "content": "active", "status": "in_progress", "generation_token": "current"},
        {"id": "old", "content": "old", "status": "completed", "generation_token": "old"},
    ]}
    rail = object.__new__(stream_event_rail.JiuSwarmStreamEventRail)
    rail._member_name = "member"  # Avoid unrelated perf recording.
    session = _FakeSession()
    session.update_state({TODO_GENERATION_TOKEN_SESSION_KEY: "current"})
    emitted = []

    async def write_stream(schema):
        emitted.append(schema.payload)

    session.write_stream = write_stream
    if source == "loaded":
        class Loaded(list):
            pass

        items = Loaded(snapshot["todos"])
        items.snapshot = decode_snapshot(snapshot)

        async def load(_session_id):
            return items

        monkeypatch.setattr(rail, "_get_todo_tool", lambda: SimpleNamespace(load_todos=load))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        await rail._emit_todo_updated(
            session, "sess-1", tool_name=tool_name, arguments=json.dumps(arguments),
            snapshot=snapshot if source == "committed" else None,
        )
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert len(emitted) == 1
    assert [row["id"] for row in emitted[0]["todos"]] == expected_ids
    assert [row["status"] for row in emitted[0]["todos"]] == expected_status
    assert emitted[0].get("todo_meta") == ({"plan_id": "P", "revision": 8} if mode == "versioned" else None)
    assert [row["id"] for row in snapshot["todos"]] == ["R", "A", "old"]


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
