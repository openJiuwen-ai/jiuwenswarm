# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression guard: todo generation token must land on the runtime session.

``prepare_stale_todo_cleanup_for_request`` bumps the todo generation token at
the start of a fresh (non-resume) turn (request isolation, P1-2). Old-generation
todo entries are then filtered out by every broadcast channel (todo.updated /
task.update) and by ``todo_list`` — replacing the previous skip-flag +
stale/pre/current id-snapshot layers.

Originally the skip flag was set on the **throwaway session** built inside the
helper (``create_agent_session`` for the disk-only cancel). ``before_invoke``
reads ``ctx.session`` — the runtime ``_interaction_session``, a *different*
object — so it saw ``skip_invoke=False`` and the stale todo list replayed into
the new turn's first frame ("残留任务列表又被弹出").

This test pins the token-era equivalent: pass ``runtime_session`` and bump the
token on **it** (and, via the flag proxy, on the throwaway for checkpointer
persistence).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    TODO_RESUME_SNAPSHOT_PENDING_KEY,
    get_todo_generation_token,
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


def _request(query: str = "你好，帮我查个产品", mode: str = "agent") -> Any:
    return SimpleNamespace(
        request_id="req-1",
        session_id="sess-1",
        params={"mode": mode, "query": query},
    )


def _patch_session_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    """Patch create_agent_session / post_agent_execute inside the helper."""
    throwaway = _FakeSession("sess-1")

    import jiuwenswarm.server.runtime.agent_adapter.stale_todo_cleanup_helpers as mod

    async def _noop_post_agent_execute(_session: Any) -> None:
        return None

    monkeypatch.setattr(mod, "create_agent_session", lambda **kw: throwaway)
    monkeypatch.setattr(mod, "post_agent_execute_for_session", _noop_post_agent_execute)
    return throwaway


@pytest.mark.asyncio
async def test_generation_token_lands_on_runtime_session(monkeypatch) -> None:
    """The bumped token must be visible on ``runtime_session``.

    This is the object ``TaskExecutionRail.before_invoke`` and
    ``StreamEventRail._emit_todo_updated`` read via ``ctx.session``; a token set
    only on the throwaway is invisible and old-generation todos re-broadcast.
    """
    throwaway = _patch_session_factory(monkeypatch)
    runtime_session = _FakeSession("sess-1")

    ok = await prepare_stale_todo_cleanup_for_request(
        _request(),
        agent_card=SimpleNamespace(),
        runtime_session=runtime_session,
    )

    assert ok is True
    runtime_token = get_todo_generation_token(runtime_session)
    assert runtime_token, (
        "generation token must be bumped on runtime_session "
        "(_interaction_session); broadcast layers read ctx.session which is "
        "the runtime session — a token set only on the throwaway leaves "
        "old-generation todos unfiltered and they replay into the new turn"
    )
    # Proxy dual-write: the throwaway (checkpointer-backed in production)
    # carries the same token so it survives process restarts.
    assert get_todo_generation_token(throwaway) == runtime_token
    # The resume snapshot pending flag is cleared: the old task is superseded.
    assert runtime_session.get_state(TODO_RESUME_SNAPSHOT_PENDING_KEY) is not True


@pytest.mark.asyncio
async def test_resume_turn_keeps_generation_token(monkeypatch) -> None:
    """Resume / supplement / answer turns must NOT bump the token.

    Their todos were stamped with the current generation and must stay visible.
    """
    _patch_session_factory(monkeypatch)
    runtime_session = _FakeSession("sess-1")
    runtime_session.update_state({"todo_generation_token": "gen-current"})

    for params in (
        {"mode": "agent", "query": "继续"},
        {"mode": "agent", "query": "帮我做另一件事", "is_supplement": True},
        {"mode": "agent", "query": "帮我做另一件事", "answers": [{"selected_options": ["a"]}]},
        {"mode": "chat", "query": "你好"},
    ):
        request = SimpleNamespace(
            request_id="req-1",
            session_id="sess-1",
            params=params,
        )
        ok = await prepare_stale_todo_cleanup_for_request(
            request,
            agent_card=SimpleNamespace(),
            runtime_session=runtime_session,
        )
        assert ok is False, f"turn must not bump generation: {params}"
        assert get_todo_generation_token(runtime_session) == "gen-current", (
            f"token must be preserved on continuation turns: {params}"
        )


@pytest.mark.asyncio
async def test_fresh_turn_replaces_previous_token(monkeypatch) -> None:
    """A fresh (non-resume) turn must replace the previous generation token."""
    _patch_session_factory(monkeypatch)
    runtime_session = _FakeSession("sess-1")
    runtime_session.update_state({"todo_generation_token": "gen-old"})

    ok = await prepare_stale_todo_cleanup_for_request(
        _request(),
        agent_card=SimpleNamespace(),
        runtime_session=runtime_session,
    )

    assert ok is True
    new_token = get_todo_generation_token(runtime_session)
    assert new_token and new_token != "gen-old", (
        "fresh user turn must start a new todo generation so entries stamped "
        "with the previous token are filtered from broadcasts"
    )
