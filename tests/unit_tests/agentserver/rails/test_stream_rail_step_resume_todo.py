# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Step-resume turns must re-emit todo.updated.

A silently restarted session (platform relaunches with the original session
id) goes through ``pre_run`` → ``run_one_round`` → the outer BEFORE_INVOKE.
Nothing in that path fetches history, so the frontend never learns about the
todo list the resumed context carries. ``StreamEventRail.before_invoke``
covers exactly that case; opening the session by hand still goes through
``history.get``, which keeps emitting on its own.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import InvokeInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _FakeSession:
    def __init__(self, resume) -> None:
        self._resume = resume

    def get_session_id(self) -> str:
        return "conv-1"

    def peek_agent_step_resume(self):
        return self._resume

    def get_state(self, key=None):
        return None

    def update_state(self, data) -> None:
        return None


def _ctx(session) -> SimpleNamespace:
    return SimpleNamespace(
        agent=None,
        session=session,
        extra={},
        inputs=InvokeInputs(query="q", conversation_id="conv-1"),
    )


@pytest.mark.parametrize("resume,expected", [
    (SimpleNamespace(iteration=2, query="q"), ["conv-1"]),
    (None, []),
    (MagicMock(), []),
])
@pytest.mark.asyncio
async def test_before_invoke_emits_todo_updated_only_on_step_resume(monkeypatch, resume, expected):
    rail = JiuSwarmStreamEventRail()
    emitted: list[str] = []

    async def _record(session, session_id, **kwargs):
        emitted.append(session_id)

    monkeypatch.setattr(rail, "_emit_todo_updated", _record)
    monkeypatch.setattr(rail, "_rebind_runtime_cwd", lambda **kwargs: None)

    await rail.before_invoke(_ctx(_FakeSession(resume)))

    assert emitted == expected
