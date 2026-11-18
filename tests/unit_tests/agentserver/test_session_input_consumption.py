"""Only final Host admission advances the effective context's history boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage, UserMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs, UserMessageInputs
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness.task_loop.loop_queues import LoopQueues
from openjiuwen.harness.task_loop.task_loop_controller import TaskLoopController

from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    QueuedSessionInput, SessionInputGuard, enqueue_bound_session_input,
)
from tests.unit_tests.test_context_snapshots import native_session, stored, user


@pytest.fixture
async def admission(stored, monkeypatch):
    _, delegate = await native_session(stored, monkeypatch)
    engine = ContextEngine()
    await engine.create_context(session=stored.session, history_messages=[UserMessage(content="prior")])
    queues = LoopQueues()
    controller = TaskLoopController()
    controller.set_event_handler(SimpleNamespace(interaction_queues=queues))
    owner = SimpleNamespace(
        react_agent=SimpleNamespace(), loop_session=stored.session,
        active_round=SimpleNamespace(work=SimpleNamespace(kind="user", request_id="current")),
        has_output_stream=lambda: True, loop_controller=controller,
    )
    guard = SessionInputGuard(owner)
    guard._session = stored.session
    monkeypatch.setattr(stored.session, "write_stream", AsyncMock())
    ctx = AgentCallbackContext(
        agent=owner.react_agent, session=stored.session,
        context=engine.get_context(session_id=stored.sid),
        inputs=UserMessageInputs(parts=["input"], source="query"),
    )
    return SimpleNamespace(guard=guard, owner=owner, ctx=ctx, queues=queues, stored=stored,
        engine=engine, delegate=delegate)


@pytest.mark.asyncio
async def test_query_consumption_needs_no_outer_extra_or_permission_carrier(admission):
    a = admission
    assert not a.ctx.extra
    assert not getattr(a.ctx.context, "history_recovery_covered", set())
    await a.guard.on_user_message(a.ctx)
    assert a.ctx.context.history_recovery_covered == {"current"}
    assert "_history_consumed_requests" not in a.ctx.extra


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, 123, [], "", " ", "x" * 257])
async def test_invalid_request_identity_never_enters_snapshot_coverage(admission, request_id):
    a = admission
    a.owner.active_round.work.request_id = request_id
    await a.guard.on_user_message(a.ctx)
    assert not getattr(a.ctx.context, "history_recovery_covered", set())


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["agent", "session", "round", "work", "goal", "empty", "resume"])
async def test_unproven_query_is_not_marked_consumed(admission, fault):
    a = admission
    if fault == "agent":
        a.ctx.agent = object()
    elif fault == "session":
        a.owner.loop_session = object()
    elif fault == "round":
        a.owner.active_round = None
    elif fault == "work":
        a.owner.active_round.work = None
    elif fault == "goal":
        a.owner.active_round.work.kind = "goal"
    elif fault == "empty":
        a.ctx.inputs.parts.clear()
    else:
        a.ctx.inputs.source = "resume"
    a.ctx.extra["_history_incoming_request"] = "must-not-be-used"
    await a.guard.on_user_message(a.ctx)
    assert not getattr(a.ctx.context, "history_recovery_covered", set())


def enqueue(a, request_id, text="same text"):
    request = SimpleNamespace(request_id=request_id, session_id=a.stored.sid, params={})
    return enqueue_bound_session_input(
        a.owner, a.owner.active_round, request,
        SendInputRequest(request_id=request_id, inputs={"query": text}, mode=InputDispatchMode.STEER),
    )


@pytest.mark.asyncio
async def test_actual_host_entries_are_covered_only_after_final_admission(admission):
    a = admission
    first, second = enqueue(a, "first"), enqueue(a, "second")
    await a.guard.publish_input_received(first)
    await a.guard.publish_input_received(second)
    a.ctx.bind_steering_queue(a.queues.steering)
    parts = a.ctx.drain_steering(limit=1)
    assert parts == [first] and isinstance(parts[0], QueuedSessionInput)
    a.ctx.inputs = UserMessageInputs(parts=parts, source="steering")
    await a.guard.boundary_guard.on_user_message(a.ctx)
    # A prior rail may put a drained input back before final admission.
    a.queues.push_steer(a.ctx.inputs.parts.pop())
    await a.guard.on_user_message(a.ctx)
    assert not getattr(a.ctx.context, "history_recovery_covered", set())
    a.ctx.inputs = UserMessageInputs(parts=a.ctx.drain_steering(limit=1), source="steering")
    await a.guard.boundary_guard.on_user_message(a.ctx)
    await a.guard.on_user_message(a.ctx)
    assert a.ctx.context.history_recovery_covered == {"second"}
    a.ctx.inputs = UserMessageInputs(parts=a.ctx.drain_steering(limit=1), source="steering")
    await a.guard.boundary_guard.on_user_message(a.ctx)
    await a.guard.on_user_message(a.ctx)
    assert a.ctx.context.history_recovery_covered == {"first", "second"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["pending", "publication", "stale", "plain", "untyped"])
async def test_rejected_or_unowned_supplements_are_not_claimed(admission, fault):
    a = admission
    part = enqueue(a, "supplement")
    await a.guard.publish_input_received(part)
    if fault == "pending":
        part.boundary_ready.clear()
    elif fault == "publication":
        part.boundary_error = RuntimeError("publication failed")
    elif fault == "stale":
        part.delivery_round = object()
    elif fault == "plain":
        part = str(part)
    else:
        class UntrustedText(str):
            request_id = "not-a-host-entry"
        part = UntrustedText("same text")
    a.ctx.inputs = UserMessageInputs(parts=[part], source="steering")
    await a.guard.on_user_message(a.ctx)
    assert not getattr(a.ctx.context, "history_recovery_covered", set())


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["query", "steering"])
@pytest.mark.parametrize("fails", [False, True])
async def test_cross_session_consumption_requires_successful_conversion(admission, monkeypatch, source, fails):
    from agent_gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
    a = admission
    marker = {"message_id": "message", "source_session_id": "sender", "hop_count": 1}
    part = "agent data"
    expected = "current"
    if source == "query":
        a.ctx.extra["run_context"] = {"extra": {SESSION_MESSAGE_INTERNAL_KEY: marker}}
    else:
        part = enqueue(a, "supplement", text="agent data")
        part.cross_session = marker
        await a.guard.publish_input_received(part)
        expected = "supplement"
    a.ctx.inputs = UserMessageInputs(parts=[part], source=source)
    if fails:
        monkeypatch.setattr(a.ctx.context, "add_messages", AsyncMock(side_effect=OSError("failed conversion")))
        with pytest.raises(OSError):
            await a.guard.on_user_message(a.ctx)
        assert not getattr(a.ctx.context, "history_recovery_covered", set())
    else:
        await a.guard.on_user_message(a.ctx)
        assert a.ctx.inputs.parts == []
        assert a.ctx.context.history_recovery_covered == {expected}
        messages = a.ctx.context.get_messages()
        assert messages[-1].role == "tool" and "agent data" in messages[-1].content


def continuation_result(a):
    query = InteractiveInput()
    query.answer("interrupt", {"approved": True})
    a.owner.active_round.work.query = query
    call = ToolCall(id="result-call", type="function", name="probe", arguments="{}")
    message = ToolMessage(content="actual result", tool_call_id=call.id, metadata={"preserved": "yes"})
    a.ctx.inputs = ToolCallInputs(tool_call=call, tool_msg=message)
    return call, message


@pytest.mark.asyncio
async def test_only_a_continuation_result_installed_in_the_native_window_is_covered(admission):
    import json
    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
    a = admission
    call, message = continuation_result(a)
    a.stored.write([user("queued", "UNCONSUMED-INPUT")])
    await a.guard.after_tool_call(a.ctx)
    assert message.metadata == {"preserved": "yes", "jiuwenswarm_request_ids": ["current"]}
    await snapshots.save_context_snapshot(context_engine=a.engine, session=a.stored.session)
    assert not a.ctx.context.history_recovery_covered
    await a.ctx.context.add_messages([AssistantMessage(content="", tool_calls=[call]), message])
    await snapshots.save_context_snapshot(context_engine=a.engine, session=a.stored.session)
    assert a.ctx.context.history_recovery_covered == {"current"}
    assert a.ctx.context.history_recovery_cursor.offset == 0
    with a.stored.path.open("a", encoding="utf-8") as history:
        for row in [
            {"role": "assistant", "event_type": "chat.tool_call", "request_id": "current",
             "tool_call": {"name": "probe", "arguments": "{}", "tool_call_id": call.id}},
            {"role": "assistant", "event_type": "chat.tool_result", "request_id": "current",
             "tool_call_id": call.id, "content": "actual result"},
        ]:
            history.write(json.dumps(row) + "\n")
    restored = ContextEngine()
    assert await snapshots.restore_checkpoint_context(
        context_engine=restored, session=a.stored.session, processors=[], before_request_id=None,
    )
    messages = restored.get_context(session_id=a.stored.sid).get_messages()
    assert len([message for message in messages if getattr(message, "tool_call_id", None) == call.id]) == 1
    assert len([message for message in messages if message.content == "UNCONSUMED-INPUT"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced", [False, True])
async def test_discarded_result_identity_does_not_cover_a_different_native_window(admission, replaced):
    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
    a = admission
    call, discarded = continuation_result(a)
    a.stored.write([user("queued", "UNCONSUMED-INPUT")])
    await snapshots.save_context_snapshot(context_engine=a.engine, session=a.stored.session)
    await a.guard.after_tool_call(a.ctx)
    assert discarded.metadata["jiuwenswarm_request_ids"] == ["current"]
    # Core may omit a result or substitute a tracker note after a rail fails.
    # The snapshot owner must inspect the installed window, not the old object.
    if replaced:
        await a.ctx.context.add_messages([
            AssistantMessage(content="", tool_calls=[call]),
            ToolMessage(content="replacement result", tool_call_id=call.id),
        ])
    await a.stored.session.commit_interrupt()
    saved = snapshots.load_checkpoint_snapshot(a.stored.sid, a.delegate.disk[snapshots.SNAPSHOT_KEY])
    assert not saved.covered_requests and saved.cursor.offset == 0
    assert not a.ctx.context.history_recovery_covered
    assert not any("current" in (message.metadata or {}).get("jiuwenswarm_request_ids", [])
        for message in saved.contexts["default_context_id"]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["agent", "session", "round", "goal", "plain", "empty", "raw", "id", "call", "missing"])
async def test_unproven_continuation_result_carries_no_identity(admission, fault):
    a = admission
    _, message = continuation_result(a)
    if fault == "agent":
        a.ctx.agent = object()
    elif fault == "session":
        a.owner.loop_session = object()
    elif fault == "round":
        a.owner.active_round = None
    elif fault == "goal":
        a.owner.active_round.work.kind = "goal"
    elif fault == "plain":
        a.owner.active_round.work.query = "not an answer"
    elif fault == "empty":
        a.owner.active_round.work.query = InteractiveInput()
    elif fault == "raw":
        a.owner.active_round.work.query = InteractiveInput(raw_inputs="text")
    elif fault == "id":
        a.owner.active_round.work.request_id = "x" * 257
    elif fault == "call":
        message.tool_call_id = "another-call"
    else:
        a.ctx.inputs.tool_msg = None
    await a.guard.after_tool_call(a.ctx)
    assert message.metadata == {"preserved": "yes"}
    assert not getattr(a.ctx.context, "history_recovery_covered", set())


@pytest.mark.asyncio
async def test_completed_continuation_identity_is_not_reinserted_by_native_saves(admission):
    from jiuwenswarm.common.schema.message import SESSION_REQUEST_COMPLETED_EVENT
    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
    a = admission
    call, message = continuation_result(a)
    await a.guard.after_tool_call(a.ctx)
    await a.ctx.context.add_messages([AssistantMessage(content="", tool_calls=[call]), message])
    await snapshots.save_context_snapshot(context_engine=a.engine, session=a.stored.session)
    assert a.ctx.context.history_recovery_covered == {"current"}
    a.stored.write([{"role": "assistant", "request_id": "current", "event_type": SESSION_REQUEST_COMPLETED_EVENT}])
    for _ in range(3):
        await snapshots.save_context_snapshot(context_engine=a.engine, session=a.stored.session)
        assert not a.ctx.context.history_recovery_covered


@pytest.mark.asyncio
async def test_failed_native_interrupt_commit_keeps_previous_result_reference(admission):
    from copy import deepcopy
    from jiuwenswarm.server.runtime.session import context_snapshot as snapshots
    a = admission
    await snapshots.save_context_snapshot(context_engine=a.engine, session=a.stored.session)
    previous = deepcopy(a.delegate.disk)
    call, message = continuation_result(a)
    await a.guard.after_tool_call(a.ctx)
    await a.ctx.context.add_messages([AssistantMessage(content="", tool_calls=[call]), message])
    a.delegate.fail = True
    with pytest.raises(OSError, match="controlled commit failure"):
        await a.stored.session.commit_interrupt()
    assert a.delegate.disk == previous
    assert a.stored.session.get_state(snapshots.SNAPSHOT_KEY) == previous[snapshots.SNAPSHOT_KEY]
    a.delegate.fail = False
    await a.stored.session.commit_interrupt()
    saved = snapshots.load_checkpoint_snapshot(a.stored.sid, a.delegate.disk[snapshots.SNAPSHOT_KEY])
    assert "current" in saved.covered_requests
    assert len([message for message in saved.contexts["default_context_id"]["messages"]
        if getattr(message, "tool_call_id", None) == call.id]) == 1
