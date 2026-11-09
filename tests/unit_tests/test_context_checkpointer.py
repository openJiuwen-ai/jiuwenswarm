"""Host-only preparation must follow native commit, interruption, and ownership."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import UserMessage, ToolMessage

from jiuwenswarm.runtime.context_checkpointer import bind_context_checkpoint
from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError
from tests.unit_tests.test_context_snapshots import stored, user, save, native_session


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_native_commit_and_interrupt_prepare_matching_state(stored, monkeypatch, interrupted):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old"), user("pending")])
    engine = await save(stored, [UserMessage(content="old")])
    context = engine.get_context(session_id=stored.sid)
    context.set_messages([UserMessage(content="admitted input")])
    context.history_recovery_covered.add("pending")
    stored.session.update_state({"pending_permission": {"call_id": "write-1"}})
    await engine.save_contexts(stored.session)
    if interrupted:
        await stored.session.commit_interrupt()
    else:
        await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    assert loaded.contexts["default_context_id"]["messages"][0].content == "admitted input"
    assert delegate.disk["context"]["default_context_id"]["messages"][0].content == "admitted input"
    assert delegate.disk["pending_permission"]["call_id"] == "write-1"


@pytest.mark.asyncio
async def test_failed_native_commit_keeps_previous_reference(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="committed")])
    previous = copy.deepcopy(delegate.disk)
    engine.get_context(session_id=stored.sid).set_messages([UserMessage(content="not committed")])
    delegate.fail = True
    with pytest.raises(OSError, match="controlled commit failure"):
        await stored.session.commit()
    assert delegate.disk == previous
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, previous[snapshots.SNAPSHOT_KEY])
    assert loaded.contexts["default_context_id"]["messages"][0].content == "committed"


@pytest.mark.asyncio
async def test_stale_session_cannot_replace_new_owner_checkpoint(stored, monkeypatch):
    _, first = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    old_session = stored.session
    engine = await save(stored, [UserMessage(content="first owner")])
    previous = copy.deepcopy(first.disk)
    _, second = await native_session(stored, monkeypatch)
    next_engine = ContextEngine()
    await next_engine.create_context(session=stored.session, history_messages=[UserMessage(content="second owner")])
    bind_context_checkpoint(next_engine, stored.session)
    with pytest.raises(HistoryReplayError) as raised:
        await old_session.commit()
    assert raised.value.code == "HISTORY_RESTORE_CHANGED"
    assert first.disk == previous
    assert not second.disk
    assert engine.get_context(session_id=stored.sid).get_messages()[0].content == "first owner"


@pytest.mark.asyncio
async def test_native_memory_offloads_are_preserved_within_budget(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="summary")])
    context = engine.get_context(session_id=stored.sid)
    context.offload_messages("handle", [ToolMessage(content="ORIGINAL", tool_call_id="call")])
    await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    message = loaded.contexts["default_context_id"]["offload_messages"]["handle"][0]
    assert isinstance(message, ToolMessage) and message.content == "ORIGINAL"
    assert message.tool_call_id == "call"


@pytest.mark.asyncio
async def test_large_native_offloads_fail_before_checkpoint_publication(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="summary")])
    previous = copy.deepcopy(delegate.disk)
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 2048)
    engine.get_context(session_id=stored.sid).offload_messages("large", [UserMessage(content="x" * 8192)])
    with pytest.raises(HistoryReplayError) as raised:
        await stored.session.commit()
    assert raised.value.code == "HISTORY_RESTORE_LIMIT"
    assert delegate.disk == previous
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]


@pytest.mark.asyncio
async def test_consumed_inputs_need_no_new_core_metadata_field(stored, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail

    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old"), user("pending")])
    engine = await save(stored, [UserMessage(content="old")])
    context = engine.get_context(session_id=stored.sid)
    rail = JiuSwarmStreamEventRail()
    callback = SimpleNamespace(inputs=SimpleNamespace(source="query", parts=[]), context=context,
                               extra={"_history_incoming_request": "pending"})
    await rail.on_user_message(callback)
    # Empty parts can be a deliberate admission filter. They have still been
    # drained by Core and must not become a newly replayed request on restart.
    await stored.session.commit()
    loaded = snapshots.load_checkpoint_snapshot(stored.sid, delegate.disk[snapshots.SNAPSHOT_KEY])
    assert loaded.cursor.offset == stored.path.stat().st_size


@pytest.mark.asyncio
async def test_inherited_owner_does_not_capture_a_child_session(stored, monkeypatch):
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.single_agent import AgentCard
    from jiuwenswarm.runtime.context_checkpointer import OWNER_KEY

    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="parent")])
    child = create_agent_session(session_id=stored.sid + "-child", card=AgentCard(id="child", name="child"))
    await child.pre_run(inputs=None)
    child.update_state({OWNER_KEY: stored.session.get_state(OWNER_KEY), "child_data": "kept"})
    await child.commit()
    assert delegate.disk["child_data"] == "kept"
    assert snapshots.SNAPSHOT_KEY not in delegate.disk
    assert engine.get_context(session_id=stored.sid).get_messages()[0].content == "parent"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_concurrent_commits_keep_frozen_window_and_order(stored, monkeypatch, explicit):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="initial")])
    entered, release = asyncio.Event(), asyncio.Event()
    original = delegate.post_agent_execute
    recorded = []

    async def delayed(session):
        if not recorded:
            recorded.append("entered")
            entered.set()
            await release.wait()
        await original(session)
        disk = copy.deepcopy(delegate.disk)
        loaded = snapshots.load_checkpoint_snapshot(stored.sid, disk[snapshots.SNAPSHOT_KEY])
        window = disk["context"]["default_context_id"]["messages"][0].content
        assert loaded.contexts["default_context_id"]["messages"][0].content == window
        recorded.append(window)

    monkeypatch.setattr(delegate, "post_agent_execute", delayed)
    context = engine.get_context(session_id=stored.sid)
    context.set_messages([UserMessage(content="first")])
    first = asyncio.create_task(snapshots.save_context_snapshot(context_engine=engine, session=stored.session)
                                if explicit else stored.session.commit())
    await asyncio.wait_for(entered.wait(), 5)
    context.set_messages([UserMessage(content="second")])
    await engine.save_contexts(stored.session)
    second = asyncio.create_task(stored.session.commit())
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert recorded == ["entered", "first", "second"]


@pytest.mark.asyncio
async def test_cancelled_commit_preserves_committed_reference_and_releases_lock(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    stored.write([user("old")])
    engine = await save(stored, [UserMessage(content="committed")])
    previous = copy.deepcopy(delegate.disk)
    entered = asyncio.Event()
    original = delegate.post_agent_execute

    async def blocked(session):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(delegate, "post_agent_execute", blocked)
    engine.get_context(session_id=stored.sid).set_messages([UserMessage(content="not yet committed")])
    task = asyncio.create_task(stored.session.commit())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert delegate.disk == previous
    assert stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]
    monkeypatch.setattr(delegate, "post_agent_execute", original)
    await asyncio.wait_for(stored.session.commit(), 5)
    assert delegate.disk["context"]["default_context_id"]["messages"][0].content == "not yet committed"
