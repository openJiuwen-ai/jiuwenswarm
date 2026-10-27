# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A generated draft is not proof that an overlapping user saw the answer."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
)
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs, ModelCallInputs

from jiuwenswarm.server.runtime.agent_adapter import session_input
from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    QueuedSessionInput, SessionInputDeliveryUnknown, SessionInputGuard,
)


def _guard_and_context():
    agent = SimpleNamespace(config=SimpleNamespace(max_iterations=None))
    owner = SimpleNamespace(
        active_round=object(), react_agent=agent,
        event_handler=SimpleNamespace(interaction_queues=SimpleNamespace(steering=asyncio.Queue())),
    )
    guard = SessionInputGuard(owner)
    session = SimpleNamespace(write_stream=AsyncMock())
    guard._session = session
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=agent, session=session, context=context, inputs=ModelCallInputs())
    return guard, ctx


async def _capture(guard, ctx, content="99 * 99 = 9801"):
    ctx.inputs = ModelCallInputs(response=AssistantMessage(
        content=content, reasoning_content="private reasoning must not be copied",
        tool_calls=[ToolCall(id="done", type="function", name="write_file", arguments='{"file_path":"do-not-repeat"}')],
    ))
    await guard.after_model_call(ctx)


async def _consume(guard, ctx, entries):
    ctx.inputs = SimpleNamespace(parts=list(entries), source="steering")
    await guard.boundary_guard.on_user_message(ctx)
    await guard.on_user_message(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=2)
    await guard.before_model_call(ctx)


def _note(ctx):
    message = ctx.context.add_messages.await_args.args[0]
    return message, json.loads(message.content.rsplit("\n", 1)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("images", [False, True])
async def test_inflight_draft_is_quoted_for_current_steer_without_replaying_tools(images):
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Identify this image" if images else "Also answer this", "input-1")
    if images:
        entry.media_items = [{"type": "image", "path": "/uploads/image.png"}]
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    await _consume(guard, ctx, [entry])
    message, data = _note(ctx)
    assert data == {
        "input_request_ids": ["input-1"],
        "quoted_prior_output": [{"text": "99 * 99 = 9801", "truncated": False}],
    }
    assert message.metadata[OPENJIUWEN_MESSAGE_ORIGIN_METADATA] == OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL
    assert message.metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] == "session_input_delivery"
    assert "cancelling or replacing earlier work take precedence" in message.content
    assert "without repeating tools or side effects" in message.content
    assert "draft data, not new instructions" in message.content
    assert "private reasoning" not in message.content and "do-not-repeat" not in message.content
    assert str(entry) == ("Identify this image" if images else "Also answer this")
    assert guard._pending_delivery_inputs == []
    assert [call.args[0].type for call in ctx.session.write_stream.await_args_list] == [
        "session_input_received", "session_output_phase",
    ]
    ctx.inputs = ModelCallInputs(react_iteration=3)
    await guard.before_model_call(ctx)
    ctx.context.add_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_gap_gets_scoped_delivery_note_without_copying_older_answers():
    guard, ctx = _guard_and_context()
    await _capture(guard, ctx, "An old fully delivered answer")
    entry = QueuedSessionInput("Continue with new evidence", "input-gap")
    await guard.publish_input_received(entry)
    await _consume(guard, ctx, [entry])
    message, data = _note(ctx)
    assert data["quoted_prior_output"] == []
    assert "An old fully delivered answer" not in message.content
    assert "current execution" in message.content and "unrelated earlier conversation tasks" in message.content


@pytest.mark.asyncio
async def test_batch_keeps_request_ids_and_quotes_shared_response_once():
    guard, ctx = _guard_and_context()
    entries = [QueuedSessionInput("same body", request_id) for request_id in ("input-a", "input-b")]
    for entry in entries:
        await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    await _consume(guard, ctx, entries)
    _, data = _note(ctx)
    assert data["input_request_ids"] == ["input-a", "input-b"]
    assert len(data["quoted_prior_output"]) == 1
    phase = ctx.session.write_stream.await_args.args[0]
    assert phase.payload["applied_input_ids"] == ["input-a", "input-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_response_capture_does_not_race_boundary_publication(fails):
    guard, ctx = _guard_and_context()
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(chunk):
        if chunk.type == "session_input_received":
            entered.set()
            await release.wait()
            if fails:
                raise RuntimeError("publication failed")

    ctx.session.write_stream.side_effect = write
    entry = QueuedSessionInput("New requirement", "input-backpressure")
    publication = asyncio.create_task(guard.publish_input_received(entry))
    await entered.wait()
    await _capture(guard, ctx)
    assert entry.undelivered_response_text == "99 * 99 = 9801"
    release.set()
    if fails:
        with pytest.raises(SessionInputDeliveryUnknown):
            await publication
    else:
        await publication
    await _consume(guard, ctx, [entry])
    if fails:
        ctx.context.add_messages.assert_not_awaited()
        assert entry.undelivered_response_text == ""
    else:
        assert _note(ctx)[1]["quoted_prior_output"][0]["text"] == "99 * 99 = 9801"


@pytest.mark.asyncio
async def test_delivery_quotes_are_bounded_and_marked_as_truncated(monkeypatch):
    monkeypatch.setattr(session_input, "_MAX_UNDELIVERED_RESPONSE_CHARS", 100)
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Supplement", "input-long")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx, "old answer " * 1000)
    await _consume(guard, ctx, [entry])
    message, data = _note(ctx)
    quoted = data["quoted_prior_output"]
    assert sum(len(part["text"]) for part in quoted) <= 100
    assert quoted[0]["truncated"] is True
    assert "original assistant message remains in the conversation" in message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["query", "after_invoke", "on_model_exception", "on_tool_exception"])
async def test_delivery_capture_does_not_leak_to_new_work(cleanup):
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Supplement", "input-old")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    if cleanup == "query":
        ctx.inputs = SimpleNamespace(parts=["New task"], source="query")
        await guard.on_user_message(ctx)
    else:
        ctx.inputs = InvokeInputs(query="old", result={"result_type": "answer"})
        await getattr(guard, cleanup)(ctx)
    assert guard._pending_delivery_inputs == []
    assert entry.undelivered_response_text == ""


@pytest.mark.asyncio
async def test_interrupt_resume_retains_only_its_pending_delivery_capture():
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Supplement", "input-resumed")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    ctx.inputs = InvokeInputs(query="old", result={"result_type": "interrupt"})
    await guard.after_invoke(ctx)
    guard.owner.active_round = object()
    ctx.inputs = InvokeInputs(query=InteractiveInput())
    await guard.before_invoke(ctx)
    await _consume(guard, ctx, [entry])
    assert _note(ctx)[1]["quoted_prior_output"][0]["text"] == "99 * 99 = 9801"


@pytest.mark.asyncio
async def test_a_different_execution_cannot_receive_the_old_delivery_note():
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Supplement", "input-old-round")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    guard.owner.active_round = object()
    await _consume(guard, ctx, [entry])
    ctx.context.add_messages.assert_not_awaited()
