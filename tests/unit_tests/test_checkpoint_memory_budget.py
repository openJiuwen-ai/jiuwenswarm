# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A Worker admits preparation and persistence together across member keys."""
import asyncio

import pytest
from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import UserMessage
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.inmemory import InMemoryCheckpointer
from openjiuwen.core.single_agent import AgentCard

from jiuwenswarm.runtime.checkpoint_budget import admit_context_checkpoint
from jiuwenswarm.runtime.context_checkpointer import ContextCheckpointer, bind_context_checkpoint
from jiuwenswarm.server.runtime.session import context_snapshot as snapshots, lifecycle as lc, session_history


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind", ["normal", "explicit", "interrupt"])
async def test_checkpoint_admission_precedes_copy_and_snapshot_for_other_members(monkeypatch, tmp_path, first_kind):
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    delegate = InMemoryCheckpointer()
    post = delegate.post_agent_execute
    prepared, committed = [], []

    async def persist(session):
        committed.append(session.session_id())
        if len(committed) == 1:
            entered.set()
            await release.wait()
        await post(session)

    monkeypatch.setattr(delegate, "post_agent_execute", persist)
    monkeypatch.setattr(delegate, "interrupt_agent_execute", persist)
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", ContextCheckpointer(delegate))
    members = []
    for index in range(2):
        sid = f"checkpoint-member-{index}"
        lc.claim_runtime(sid)
        path = session_history.get_write_history_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        session = create_agent_session(session_id=sid, card=AgentCard(id=f"member-{index}", name="member"))
        await session.pre_run(inputs=None)
        engine = ContextEngine()
        await engine.create_context(session=session, history_messages=[UserMessage(content=f"window-{index}")])
        save = engine.save_contexts

        async def record_save(current, save=save):
            prepared.append(current.get_session_id())
            return await save(current)

        monkeypatch.setattr(engine, "save_contexts", record_save)
        bind_context_checkpoint(engine, session)
        members.append((session, engine))
    first_session, first_engine = members[0]
    if first_kind == "explicit":
        first = asyncio.create_task(snapshots.save_context_snapshot(context_engine=first_engine, session=first_session))
    else:
        first = asyncio.create_task(first_session.commit_interrupt() if first_kind == "interrupt"
                                    else first_session.commit())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(members[1][0].commit())
        await asyncio.sleep(.05)
        assert prepared == ["checkpoint-member-0"]
        assert committed == ["checkpoint-member-0"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert prepared == ["checkpoint-member-0", "checkpoint-member-1"]
        assert committed == prepared
        for index, (session, _engine) in enumerate(members):
            loaded = snapshots.load_checkpoint_snapshot(session.get_session_id(), session.get_state(snapshots.SNAPSHOT_KEY))
            assert loaded.contexts["default_context_id"]["messages"][0].content == f"window-{index}"
    finally:
        release.set()
        for task in [first, second]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in [first, second] if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_checkpoint_budget_is_task_reentrant_but_does_not_leak_cancelled_waiters():
    entered = asyncio.Event()

    async def child():
        async with admit_context_checkpoint():
            entered.set()

    async with admit_context_checkpoint():
        async with admit_context_checkpoint():
            waiting = asyncio.create_task(child())
            await asyncio.sleep(.03)
            assert not entered.is_set(), "a child must not inherit its parent's admission"
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
    await asyncio.wait_for(child(), 1)
    assert entered.is_set()
