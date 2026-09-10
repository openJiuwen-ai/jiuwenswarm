# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard: stale-todo skip flag must land on the runtime session.

``prepare_stale_todo_cleanup_for_request`` cancels orphaned todos at the start
of a fresh (non-resume) turn and marks ``skip_invoke_task_update_sync`` so
``TaskExecutionRail.before_invoke`` captures them as stale and
``_emit_task_update_event`` filters them out of the full-snapshot broadcast.

Originally the skip flag was set on the **throwaway session** built inside the
helper (``create_agent_session`` for the disk-only cancel). ``before_invoke``
reads ``ctx.session`` — the runtime ``_interaction_session``, a *different*
object — so it saw ``skip_invoke=False``. The stale-id filter stayed empty and
the LLM's subsequent ``todo_modify`` re-broadcast the whole cancelled todo list
back to the frontend ("残留任务列表又被弹出").

This test pins the fix: pass ``runtime_session`` and set the flag on **it**.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    is_skip_invoke_task_update_sync,
)
from jiuwenswarm.server.runtime.agent_adapter.stale_todo_cleanup_helpers import (
    prepare_stale_todo_cleanup_for_request,
)


class _FakeState:
    """Minimal in-memory global-state dict mirroring Session.update/get_state."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def update_global(self, data: dict[str, Any]) -> None:
        self._store.update(data)

    def get_global(self, key: Any = None) -> Any:
        if key is None:
            return dict(self._store)
        return self._store.get(key) if isinstance(key, str) else None


class _FakeSession:
    def __init__(self, session_id: str = "sess-1") -> None:
        self._session_id = session_id
        self._state = _FakeState()
        self.pre_run_count = 0
        self.post_run_count = 0

    def get_session_id(self) -> str:
        return self._session_id

    async def pre_run(self, **kwargs: Any) -> "_FakeSession":
        self.pre_run_count += 1
        return self

    async def post_run(self) -> "_FakeSession":
        self.post_run_count += 1
        return self

    def update_state(self, data: dict[str, Any]) -> Any:
        return self._state.update_global(data)

    def get_state(self, key: Any = None) -> Any:
        return self._state.get_global(key)


class _FakeModifyTool:
    """Stand-in for TodoModifyTool — only load/cancel/save on disk state."""

    def __init__(self, todos: list[dict[str, Any]]) -> None:
        self._todos = list(todos)
        # When set, load_todos raises on the Nth call (1-based) to simulate a
        # disk/IO failure inside cancel_pending_todos_on_tool's second load.
        self.fail_load_on_call: int | None = None
        self._load_calls = 0

    async def load_todos(self, _session_id: str) -> list[Any]:
        self._load_calls += 1
        if self.fail_load_on_call is not None and self._load_calls >= self.fail_load_on_call:
            raise OSError("simulated todo.json read failure")
        # status must quack like TodoStatus (has .value); mirror _serialize_todos.
        out: list[Any] = []
        for t in self._todos:
            item = SimpleNamespace(
                id=t["id"], content=t["content"], activeForm=t["activeForm"],
                status=SimpleNamespace(value=t["status"]),
            )
            out.append(item)
        return out

    async def _cancel_todos(self, _sid: str, ids: list[str], current: list[Any]) -> None:
        for t in current:
            if getattr(t, "id", None) in ids:
                t.status = SimpleNamespace(value="cancelled")

    async def save_todos(self, _session_id: str, todos: list[Any]) -> None:
        self._todos = [
            {"id": getattr(t, "id", ""), "content": getattr(t, "content", ""),
             "activeForm": getattr(t, "activeForm", ""),
             "status": getattr(t.status, "value", t.status)}
            for t in todos
        ]


def _request(query: str = "你好，帮我查个产品", mode: str = "agent") -> Any:
    return SimpleNamespace(
        request_id="req-1",
        session_id="sess-1",
        params={"mode": mode, "query": query},
    )


@pytest.mark.asyncio
async def test_skip_flag_lands_on_runtime_session_not_throwaway(monkeypatch) -> None:
    """The skip flag must be visible on ``runtime_session`` after cleanup.

    This is the object ``TaskExecutionRail.before_invoke`` reads via
    ``ctx.session``; only if the flag is set on it does ``skip_invoke`` become
    True and ``_stale_todo_ids`` get captured.
    """
    todos = [
        {"id": "read_xlsx", "content": "读取 xlsx", "activeForm": "读取 xlsx",
         "status": "completed"},
        {"id": "analyze_data", "content": "分析数据", "activeForm": "分析数据",
         "status": "in_progress"},
        {"id": "gen_chart", "content": "生成图表", "activeForm": "生成图表",
         "status": "pending"},
    ]
    modify_tool = _FakeModifyTool(todos)
    runtime_session = _FakeSession("sess-1")

    # The helper builds its own throwaway session internally; patch the factory
    # so the throwaway is also a _FakeSession (with its own independent state).
    throwaway = _FakeSession("sess-1")

    import jiuwenswarm.server.runtime.agent_adapter.stale_todo_cleanup_helpers as mod

    async def _noop_post_agent_execute(_session: Any) -> None:
        return None

    monkeypatch.setattr(mod, "create_agent_session", lambda **kw: throwaway)
    monkeypatch.setattr(mod, "post_agent_execute_for_session", _noop_post_agent_execute)

    ok = await prepare_stale_todo_cleanup_for_request(
        _request(),
        agent_card=SimpleNamespace(),
        get_todo_modify_tool=lambda _sid: modify_tool,
        runtime_session=runtime_session,
    )

    assert ok is True
    # Throwaway session must NOT carry the flag — that was the old bug.
    assert is_skip_invoke_task_update_sync(throwaway) is False, (
        "skip flag leaked onto the throwaway session — before_invoke reads the "
        "runtime session, so the flag set here is invisible and stale todos get "
        "re-broadcast on the next todo_modify"
    )
    # Runtime session MUST carry the flag — this is what before_invoke reads.
    assert is_skip_invoke_task_update_sync(runtime_session) is True, (
        "skip flag must be set on runtime_session (_interaction_session); "
        "before_invoke reads ctx.session which is the runtime session, not the "
        "throwaway built inside the helper — setting it on the throwaway leaves "
        "skip_invoke=False and lets stale cancelled todos re-broadcast to the UI"
    )


@pytest.mark.asyncio
async def test_skip_flag_defaults_to_throwaway_when_runtime_session_omitted(monkeypatch) -> None:
    """Back-compat: callers that don't pass runtime_session still get a flag set.

    Preserves the pre-fix behavior for any caller not yet upgraded; the flag
    lands on the throwaway (invisible to before_invoke, but no worse than
    before). Upgraded callers pass runtime_session to get the real fix.
    """
    todos = [
        {"id": "t1", "content": "task1", "activeForm": "task1", "status": "in_progress"},
    ]
    modify_tool = _FakeModifyTool(todos)
    throwaway = _FakeSession("sess-1")

    import jiuwenswarm.server.runtime.agent_adapter.stale_todo_cleanup_helpers as mod

    async def _noop_post_agent_execute(_session: Any) -> None:
        return None

    monkeypatch.setattr(mod, "create_agent_session", lambda **kw: throwaway)
    monkeypatch.setattr(mod, "post_agent_execute_for_session", _noop_post_agent_execute)

    ok = await prepare_stale_todo_cleanup_for_request(
        _request(),
        agent_card=SimpleNamespace(),
        get_todo_modify_tool=lambda _sid: modify_tool,
        # runtime_session intentionally omitted
    )

    assert ok is True
    # Back-compat: flag still set on the throwaway (old behavior preserved).
    assert is_skip_invoke_task_update_sync(throwaway) is True


@pytest.mark.asyncio
async def test_skip_flag_set_even_when_cancel_crashes(monkeypatch) -> None:
    """Cancel failure must not take down the skip flag (defense in depth).

    Regression guard: the flag used to be set *after*
    ``cancel_pending_todos_on_tool``. When the cancel raised (e.g. IO error),
    the outer ``except`` swallowed it and the flag was never set — both lines
    of defense (disk cleanup + broadcast filter) failed together, and the
    stale todo list replayed into the new turn's first frame. Now the flag is
    set first; a cancel crash degrades to leftover todos on disk only.
    """
    todos = [
        {"id": "t1", "content": "task1", "activeForm": "task1", "status": "in_progress"},
    ]
    modify_tool = _FakeModifyTool(todos)
    # 1st load (helper's own load_session_todo_items) succeeds, 2nd load
    # (inside cancel_pending_todos_on_tool) blows up.
    modify_tool.fail_load_on_call = 2
    runtime_session = _FakeSession("sess-1")
    throwaway = _FakeSession("sess-1")

    import jiuwenswarm.server.runtime.agent_adapter.stale_todo_cleanup_helpers as mod

    async def _noop_post_agent_execute(_session: Any) -> None:
        return None

    monkeypatch.setattr(mod, "create_agent_session", lambda **kw: throwaway)
    monkeypatch.setattr(mod, "post_agent_execute_for_session", _noop_post_agent_execute)

    ok = await prepare_stale_todo_cleanup_for_request(
        _request(),
        agent_card=SimpleNamespace(),
        get_todo_modify_tool=lambda _sid: modify_tool,
        runtime_session=runtime_session,
    )

    # The helper still reports success: the flag (the broadcast-side defense)
    # is set, only the disk-side cancel degraded.
    assert ok is True
    assert is_skip_invoke_task_update_sync(runtime_session) is True, (
        "cancel_pending_todos_on_tool crashing must not leave the runtime "
        "session without the skip flag — before_invoke would then see "
        "skip_invoke=False, reload the leftover todo.json from disk and "
        "broadcast the whole stale list in the first frame"
    )
