# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Retired native callback namespaces must release their real context graphs."""
import asyncio
import copy
import gc
import weakref

import pytest
from openjiuwen.agent_teams.harness import TeamHarness
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.core.foundation.llm import UserMessage
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.inmemory import InMemoryCheckpointer
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent, InvokeInputs
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.harness_protocol.state import HarnessState

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.team.native_lifecycle import track_native_context_lifecycle
from jiuwenswarm.runtime.context_checkpointer import ContextCheckpointer, bind_context_checkpoint
from jiuwenswarm.server.runtime.session import context_snapshot as snapshots, lifecycle as lc, session_history


class OwnedCallback:
    def __init__(self, owner):
        self.owner = owner

    async def before_invoke(self, _ctx):
        pass


def harness():
    spec = DeepAgentSpec(card=AgentCard(id="same-card", name="member"), tools=[], rails=[],
        enable_sys_operation=False, enable_skill_discovery=False, auto_create_workspace=False)
    return TeamHarness.build(agent_spec=spec, role=TeamRole.TEAMMATE, member_name="member")


@pytest.mark.asyncio
async def test_stream_rail_installs_cleanup_for_real_recycled_natives(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", ContextCheckpointer(InMemoryCheckpointer()))
    owner = harness()
    references = []
    try:
        for generation in range(4):
            await owner.start()
            native, session = owner.inner_agent, owner.current_session()
            sid = session.get_session_id()
            lc.claim_runtime(sid)
            path = session_history.get_write_history_path(sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            rail = JiuSwarmStreamEventRail()
            rail.init(native)
            native.add_rail(rail)
            callback = AgentCallbackContext(agent=native, session=session,
                inputs=InvokeInputs(conversation_id=sid, query="model-free lifecycle"))
            # Production installs cleanup here before creating/restoring the window.
            await rail.before_invoke(callback)
            hook = OwnedCallback(native)
            await native.register_callback(AgentCallbackEvent.BEFORE_INVOKE, hook.before_invoke)
            await native.agent_callback_manager.execute(AgentCallbackEvent.BEFORE_INVOKE, callback)
            from openjiuwen.core.runner import Runner

            wrapped = Runner.callback_framework.emit_before("native-memory-probe")(hook.before_invoke)
            await wrapped(callback)
            context = await native.react_agent.context_engine.create_context(session=session,
                history_messages=[UserMessage(content=str(generation) + "x" * (256 * 1024))])
            await native.react_agent.context_engine.save_contexts(session)
            await rail.after_invoke(callback)
            references.append(weakref.ref(context))
            del native, session, rail, callback, context, hook, wrapped
            await owner.stop()
            await asyncio.sleep(0)
            gc.collect()
            assert sum(reference() is not None for reference in references) == 1
    finally:
        await owner.dispose()
    del owner
    await asyncio.sleep(0)
    gc.collect()
    assert all(reference() is None for reference in references)


@pytest.mark.asyncio
async def test_pause_and_another_native_with_same_card_keep_callbacks(monkeypatch):
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", InMemoryCheckpointer())
    first, second = harness(), harness()
    try:
        await first.start()
        await second.start()
        a, b = first.inner_agent, second.inner_agent
        calls = []

        async def observe(_ctx):
            calls.append(b)

        await b.register_callback(AgentCallbackEvent.AFTER_INVOKE, observe)
        await track_native_context_lifecycle(a)
        await track_native_context_lifecycle(a)
        await track_native_context_lifecycle(b)
        # Observe the exact callback storage through its public framework.
        from openjiuwen.core.runner import Runner

        a_prefix = a.agent_callback_manager.event_namespace
        b_prefix = b.agent_callback_manager.event_namespace
        a_before = {key for key in Runner.callback_framework.callbacks if key.startswith(a_prefix)}
        b_before = {key for key in Runner.callback_framework.callbacks if key.startswith(b_prefix)}
        assert a_before and b_before and a_prefix != b_prefix
        await a._transition(HarnessState.PAUSED)
        assert a_before.issubset(Runner.callback_framework.callbacks)
        await first.stop()
        assert not any(key.startswith(a_prefix) for key in Runner.callback_framework.callbacks)
        assert b_before.issubset(Runner.callback_framework.callbacks)
        assert second.inner_agent.state is HarnessState.IDLE
        await b.agent_callback_manager.execute(AgentCallbackEvent.AFTER_INVOKE,
            AgentCallbackContext(agent=b, session=second.current_session(), inputs=None))
        assert calls == [b]
    finally:
        await first.dispose()
        await second.dispose()


@pytest.mark.asyncio
async def test_current_native_retains_its_recoverable_window_across_persistence_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))

    class Delegate(InMemoryCheckpointer):
        fail = False
        disk = None

        async def post_agent_execute(self, session):
            if self.fail:
                raise OSError("controlled persistence failure")
            await super().post_agent_execute(session)
            self.disk = copy.deepcopy(session.state().get_global(None))

    delegate = Delegate()
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", ContextCheckpointer(delegate))
    owner = harness()
    try:
        await owner.start()
        native, session = owner.inner_agent, owner.current_session()
        await track_native_context_lifecycle(native)
        sid = session.get_session_id()
        lc.claim_runtime(sid)
        path = session_history.get_write_history_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        engine = native.react_agent.context_engine
        context = await engine.create_context(session=session, history_messages=[UserMessage(content="durable window")])
        bind_context_checkpoint(engine, session)
        await session.commit()
        assert engine.get_context(session_id=sid) is context
        delegate.fail = True
        await owner.stop()
        assert engine.get_context(session_id=sid) is context
        assert session.get_state("context")
        delegate.fail = False
        await session.commit()
        assert engine.get_context(session_id=sid) is context
        assert session.get_state("context")
        assert delegate.disk["context"]["default_context_id"]["messages"][0].content == "durable window"
        saved = snapshots.load_checkpoint_snapshot(sid, session.get_state(snapshots.SNAPSHOT_KEY))
        assert saved.contexts["default_context_id"]["messages"][0].content == "durable window"
    finally:
        delegate.fail = False
        await owner.dispose()
