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

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.foundation.store.base_kv_store import (
    BasedKVStorePipeline,
    BaseKVStore,
)
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointer
from openjiuwen.core.session.checkpointer.step_storage import StepResume
from openjiuwen.core.session.config.base import Config
from openjiuwen.core.session.internal.agent import AgentSession
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import InvokeInputs
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
from openjiuwen.harness.tools.todo import TodoListTool

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.tools.todo_resume import has_pending_step_resume


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


def _ctx(session, query="q") -> SimpleNamespace:
    return SimpleNamespace(
        agent=None,
        session=session,
        extra={},
        inputs=InvokeInputs(query=query, conversation_id="conv-1"),
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


@pytest.mark.parametrize(("query", "status", "expects_replay"), [
    ("继续执行", "in_progress", True),
    ("继续执行", "completed", False),
    ("研究新的主题", "in_progress", False),
])
@pytest.mark.parametrize("wrap_query", [False, True], ids=["plain", "web-envelope"])
@pytest.mark.asyncio
async def test_before_invoke_replays_unfinished_todos_for_text_resume(
    monkeypatch, tmp_path, query, status, expects_replay, wrap_query,
):
    rail = JiuSwarmStreamEventRail()
    fs = FsOperation("fs", OperationMode.LOCAL, "test", LocalWorkConfig())
    todo_tool = TodoListTool(SimpleNamespace(fs=lambda: fs), str(tmp_path))
    path = Path(todo_tool.get_file_path("conv-1"))
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"plan_id":"P","revision":1,"todos":'
        '[{"id":"research","content":"研究相关资料","status":"' + status + '"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(rail, "_get_todo_tool", lambda: todo_tool)
    monkeypatch.setattr(rail, "_rebind_runtime_cwd", lambda **kwargs: None)
    emitted = []

    class SessionWithStream(_FakeSession):
        async def write_stream(self, schema):
            emitted.append(schema)

    ctx = _ctx(SessionWithStream(None), query=query)
    if wrap_query:
        from openjiuwen.harness.deep_agent import DeepAgent

        from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

        agent = DeepAgent(AgentCard(agent_id="resume-test", name="resume-test"))
        ctx.inputs = agent._normalize_inputs({
            "conversation_id": "conv-1",
            "query": build_user_prompt(query, files={}, channel="web", language="zh"),
            "raw_query": query,
        })

    await rail.before_invoke(ctx)

    assert [schema.type for schema in emitted] == (["todo.updated"] if expects_replay else [])
    if expects_replay:
        assert emitted[0].payload["todos"][0]["content"] == "研究相关资料"


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_before_invoke(monkeypatch):
    """S_20 N1: an emission failure must never take the invoke down with it."""
    rail = JiuSwarmStreamEventRail()

    async def _explode(session, session_id, **kwargs):
        raise ValueError("bad todo payload")

    monkeypatch.setattr(rail, "_emit_todo_updated", _explode)
    monkeypatch.setattr(rail, "_rebind_runtime_cwd", lambda **kwargs: None)

    await rail.before_invoke(_ctx(_FakeSession(SimpleNamespace(iteration=2, query="q"))))


class _MemoryKV(BaseKVStore):
    """Minimal in-process KV store for the real-checkpointer case."""

    def __init__(self):
        self._store = {}

    async def set(self, key, value):
        self._store[key] = value

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, key):
        self._store.pop(key, None)

    async def exclusive_set(self, key, value, expiry=None):
        if key in self._store:
            return False
        self._store[key] = value
        return True

    async def exists(self, key):
        return key in self._store

    async def get_by_prefix(self, prefix):
        return {k: v for k, v in self._store.items() if k.startswith(prefix)}

    async def delete_by_prefix(self, prefix, batch_size=None):
        for key in [k for k in self._store if k.startswith(prefix)]:
            del self._store[key]

    async def mget(self, keys):
        return [self._store.get(k) for k in keys]

    async def batch_delete(self, keys, batch_size=None):
        deleted = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                deleted += 1
        return deleted

    def pipeline(self):
        async def execute(operations):
            results = []
            for operation in operations:
                if operation[0] == "set":
                    self._store[operation[1]] = operation[2]
                    results.append(None)
                elif operation[0] == "get":
                    results.append(self._store.get(operation[1]))
                elif operation[0] == "exists":
                    results.append(operation[1] in self._store)
            return results

        return BasedKVStorePipeline(execute)


@pytest.mark.asyncio
async def test_real_session_reports_pending_step_resume():
    """Duck typing must hold against the real agent-core Session/StepResume."""
    kv = _MemoryKV()
    writer_cp = PersistenceCheckpointer(kv)
    writer = AgentSession(session_id="conv-real", config=Config())
    writer.agent_id = lambda: "agent-real"
    writer_cp.begin_agent_steps(writer)
    await writer_cp.save_agent_step(writer, 0, query="q")

    # A new checkpointer on the same KV plays the process that just restarted.
    reader_cp = PersistenceCheckpointer(kv)
    session = Session(session_id="conv-real", card=AgentCard(id="agent-real", name="t"))
    session._inner._checkpointer = reader_cp
    await reader_cp.pre_agent_execute(session._inner, None)

    assert session.peek_agent_step_resume() == StepResume(iteration=0, query="q")
    assert has_pending_step_resume(session) is True

    rail = JiuSwarmStreamEventRail()
    emitted = []

    async def _record(session_, session_id, **kwargs):
        emitted.append(session_id)

    rail._emit_todo_updated = _record
    rail._rebind_runtime_cwd = lambda **kwargs: None
    await rail.before_invoke(SimpleNamespace(
        agent=None, session=session, extra={},
        inputs=InvokeInputs(query="q", conversation_id="conv-real"),
    ))
    assert emitted == ["conv-real"]
