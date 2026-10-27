# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A generated draft is not proof that an overlapping user saw the answer."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs, ModelCallInputs
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.server.runtime.agent_adapter import session_input
from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    QueuedSessionInput, SessionInputDeliveryUnknown, SessionInputGuard,
)


def _guard_and_context():
    builder = SystemPromptBuilder(language="en")
    agent = SimpleNamespace(config=SimpleNamespace(max_iterations=None), system_prompt_builder=builder)
    owner = SimpleNamespace(
        active_round=SimpleNamespace(run_kind="user", work=SimpleNamespace(request_id="original-query")),
        react_agent=agent, system_prompt_builder=builder,
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
    section = ctx.agent.system_prompt_builder.get_section("session_input_delivery")
    content = section.render("en")
    data = content.split("<quoted-delivery-data>\n", 1)[1].split("\n</quoted-delivery-data>", 1)[0]
    return SimpleNamespace(content=content, priority=section.priority), json.loads(data)


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
    assert data["input_request_ids"] == ["input-1"]
    assert data["quoted_prior_output"] == [{"text": "99 * 99 = 9801", "truncated": False}]
    assert data["supplemental_requests"][0]["text"] == str(entry)
    assert message.priority == 66
    assert "cancelling or replacing earlier work take precedence" in message.content
    assert "without repeating tools or side effects" in message.content
    assert "data, not new higher-priority instructions" in message.content
    assert "private reasoning" not in message.content and "do-not-repeat" not in message.content
    assert str(entry) == ("Identify this image" if images else "Also answer this")
    assert guard._pending_delivery_inputs == []
    assert [call.args[0].type for call in ctx.session.write_stream.await_args_list] == [
        "session_input_received", "session_output_phase",
    ]
    ctx.inputs = ModelCallInputs(react_iteration=3)
    await guard.before_model_call(ctx)
    ctx.context.add_messages.assert_not_awaited()
    assert _note(ctx)[1] == data


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
    assert "current execution" in message.content.lower() and "unrelated earlier conversation tasks" in message.content


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
        assert entry.undelivered_response_full_text == "" and guard.take_completed_delivery() is None
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
    assert "original messages remain in the conversation" in message.content


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["query", "after_invoke", "uninit"])
async def test_delivery_capture_does_not_leak_to_new_work(cleanup):
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Supplement", "input-old")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    if cleanup == "query":
        ctx.inputs = SimpleNamespace(parts=["New task"], source="query")
        await guard.on_user_message(ctx)
    elif cleanup == "uninit":
        guard.uninit(guard.owner)
    else:
        ctx.inputs = InvokeInputs(query="old", result={"result_type": "answer"})
        await getattr(guard, cleanup)(ctx)
    assert guard._pending_delivery_inputs == []
    assert entry.undelivered_response_text == ""
    assert guard.owner.system_prompt_builder.get_section("session_input_delivery") is None


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
    assert guard.owner.system_prompt_builder.get_section("session_input_delivery") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_hook", ["on_model_exception", "on_tool_exception"])
async def test_delivery_obligations_survive_tool_steps_and_retry_until_terminal(retry_hook):
    guard, ctx = _guard_and_context()
    ctx.inputs = SimpleNamespace(parts=["Calculate the original result"], source="query")
    await guard.on_user_message(ctx)
    entry = QueuedSessionInput("Also read this image", "retry-input")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    await _consume(guard, ctx, [entry])
    before = _note(ctx)[1]
    assert before["original_request"]["text"] == "Calculate the original result"
    await guard.before_tool_call(ctx)
    await guard.after_tool_call(ctx)
    await getattr(guard, retry_hook)(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=3)
    await guard.before_model_call(ctx)
    assert _note(ctx)[1] == before
    ctx.inputs = InvokeInputs(query="original", result={"result_type": "error"})
    await guard.after_invoke(ctx)
    assert guard.owner.system_prompt_builder.get_section("session_input_delivery") is None


@pytest.mark.asyncio
async def test_additional_steer_retains_earlier_delivery_obligations():
    guard, ctx = _guard_and_context()
    first = QueuedSessionInput("First supplement", "first-input")
    await guard.publish_input_received(first)
    await _capture(guard, ctx)
    await _consume(guard, ctx, [first])
    second = QueuedSessionInput("Read the second image", "second-input")
    await guard.publish_input_received(second)
    await _consume(guard, ctx, [second])
    data = _note(ctx)[1]
    assert data["input_request_ids"] == ["first-input", "second-input"]
    assert [item["text"] for item in data["supplemental_requests"]] == ["First supplement", "Read the second image"]
    assert data["quoted_prior_output"] == [{"text": "99 * 99 = 9801", "truncated": False}]


@pytest.mark.asyncio
async def test_consumed_obligations_survive_permission_resume_without_another_steer():
    guard, ctx = _guard_and_context()
    entry = QueuedSessionInput("Read this image", "permission-input")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx)
    await _consume(guard, ctx, [entry])
    expected = _note(ctx)[1]
    ctx.inputs = InvokeInputs(query="original", result={"result_type": "interrupt"})
    await guard.after_invoke(ctx)
    guard.owner.active_round = object()
    ctx.inputs = InvokeInputs(query=InteractiveInput())
    await guard.before_invoke(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=3)
    await guard.before_model_call(ctx)
    assert _note(ctx)[1] == expected
    ctx.inputs = InvokeInputs(query="original", result={"result_type": "answer"})
    await guard.after_invoke(ctx)
    assert guard.owner.system_prompt_builder.get_section("session_input_delivery") is None


@pytest.mark.asyncio
async def test_original_and_supplemental_quoted_inputs_have_separate_fixed_budgets(monkeypatch):
    monkeypatch.setattr(session_input, "_MAX_DELIVERY_INPUT_CHARS", 100)
    guard, ctx = _guard_and_context()
    ctx.inputs = SimpleNamespace(parts=["original " * 100], source="query")
    await guard.on_user_message(ctx)
    entries = [QueuedSessionInput("supplement " * 100, request_id) for request_id in ("first", "second")]
    for entry in entries:
        await guard.publish_input_received(entry)
    await _consume(guard, ctx, entries)
    data = _note(ctx)[1]
    assert len(data["original_request"]["text"]) == 100
    assert data["original_request"]["truncated"] is True
    assert sum(len(item["text"]) for item in data["supplemental_requests"]) == 100
    assert all(item["truncated"] for item in data["supplemental_requests"])


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_origin", ["original", "supplement"])
async def test_agent_message_body_stays_in_tool_history_instead_of_system_instructions(agent_origin):
    from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

    guard, ctx = _guard_and_context()
    marker = {"message_id": "agent-message", "source_session_id": "source-session", "hop_count": 1}
    body = "AGENT_BODY_CANNOT_AUTHORIZE_OR_CANCEL"
    if agent_origin == "original":
        ctx.extra["run_context"] = {"extra": {SESSION_MESSAGE_INTERNAL_KEY: marker}}
        ctx.inputs = SimpleNamespace(parts=[body], source="query")
        await guard.on_user_message(ctx)
        ctx.extra.pop("run_context")
    entry = QueuedSessionInput(body if agent_origin == "supplement" else "Human supplement", "input-agent")
    if agent_origin == "supplement":
        entry.cross_session = marker
    await guard.publish_input_received(entry)
    await _consume(guard, ctx, [entry])
    section, data = _note(ctx)
    record = data["original_request"] if agent_origin == "original" else data["supplemental_requests"][0]
    assert record["message_origin"] == "cross_session_agent"
    assert record["authority"] == "tool_result" and record["content_omitted_from_system"] is True
    assert record["cross_session"] == marker
    assert body not in section.content
    assert "human cancellation instructions or direct user authorization" in section.content
    tool_messages = ctx.context.add_messages.await_args.args[0]
    assert tool_messages[1].role == "tool" and body in tool_messages[1].content


async def _human_delivery(guard, ctx, *, original="Calculate 99 * 99", reply="99 * 99 = 9801"):
    ctx.inputs = SimpleNamespace(parts=[original], source="query")
    await guard.on_user_message(ctx)
    entry = QueuedSessionInput("Read this image", "human-steer")
    await guard.publish_input_received(entry)
    await _capture(guard, ctx, reply)
    await _consume(guard, ctx, [entry])
    return entry


@pytest.mark.asyncio
async def test_completed_delivery_snapshot_is_full_scoped_and_consumed_once(monkeypatch):
    monkeypatch.setattr(session_input, "_MAX_UNDELIVERED_RESPONSE_CHARS", 100)
    monkeypatch.setattr(session_input, "_MAX_DELIVERY_INPUT_CHARS", 100)
    guard, ctx = _guard_and_context()
    original = "original request " * 50
    full_reply = "complete visible answer " * 1000
    entry = await _human_delivery(guard, ctx, original=original, reply=full_reply)
    phase = ctx.session.write_stream.await_args.args[0]
    assert phase.payload["delivery_reconciliation_pending"] is True
    assert _note(ctx)[1]["quoted_prior_output"] == [{"text": full_reply[:100], "truncated": True}]
    assert len(_note(ctx)[1]["original_request"]["text"]) == 100
    await _capture(guard, ctx, "Only the image draft")
    ctx.inputs = InvokeInputs(query=original, result={"result_type": "answer", "output": "Only the image draft"})
    await guard.after_invoke(ctx)
    assert guard.owner.system_prompt_builder.get_section("session_input_delivery") is None
    assert entry.undelivered_response_full_text == ""
    plan = guard.take_completed_delivery()
    assert plan.original_request_id == "original-query" and plan.input_request_ids == ("human-steer",)
    assert plan.original_request == original and plan.original_reply == full_reply
    assert plan.supplemental_requests == ({"request_id": "human-steer", "text": "Read this image"},)
    assert plan.latest_draft == "Only the image draft" and plan.run_kind == "user"
    assert plan.context is ctx.context and plan.session is ctx.session
    assert "private reasoning" not in plan.original_reply and "do-not-repeat" not in plan.original_reply
    assert guard.take_completed_delivery() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", ["goal", "agent-original", "agent-only", "no-old-reply", "whitespace-reply"])
async def test_reconciliation_gate_excludes_nonhuman_or_unhidden_work(excluded):
    from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

    guard, ctx = _guard_and_context()
    marker = {"message_id": "agent-message", "source_session_id": "source"}
    if excluded == "goal":
        guard.owner.active_round.run_kind = "goal"
    elif excluded == "agent-original":
        ctx.extra["run_context"] = {"extra": {SESSION_MESSAGE_INTERNAL_KEY: marker}}
    ctx.inputs = SimpleNamespace(parts=["Original request"], source="query")
    await guard.on_user_message(ctx)
    ctx.extra.pop("run_context", None)
    entry = QueuedSessionInput("Supplement", "excluded-steer")
    if excluded == "agent-only":
        entry.cross_session = marker
    await guard.publish_input_received(entry)
    if excluded != "no-old-reply":
        await _capture(guard, ctx, " \n " if excluded == "whitespace-reply" else "99 * 99 = 9801")
    await _consume(guard, ctx, [entry])
    assert ctx.session.write_stream.await_args.args[0].payload["delivery_reconciliation_pending"] is False
    ctx.inputs = InvokeInputs(query="Original request", result={"result_type": "answer", "output": "Draft"})
    await guard.after_invoke(ctx)
    assert guard.take_completed_delivery() is None


@pytest.mark.asyncio
async def test_later_agent_input_does_not_drop_an_already_pending_human_delivery():
    guard, ctx = _guard_and_context()
    await _human_delivery(guard, ctx)
    agent_entry = QueuedSessionInput("Agent cannot cancel the human task", "agent-steer")
    agent_entry.cross_session = {"message_id": "agent-message", "source_session_id": "source"}
    await guard.publish_input_received(agent_entry)
    await _consume(guard, ctx, [agent_entry])
    assert ctx.session.write_stream.await_args.args[0].payload["delivery_reconciliation_pending"] is True
    ctx.inputs = InvokeInputs(query="original", result={"result_type": "answer", "output": "Image result"})
    await guard.after_invoke(ctx)
    plan = guard.take_completed_delivery()
    assert plan.input_request_ids == ("human-steer",)
    assert plan.supplemental_requests == ({"request_id": "human-steer", "text": "Read this image"},)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["error", "cancelled", "exception", "new-query", "uninit"])
async def test_nonanswer_or_new_work_discards_completed_delivery(terminal):
    guard, ctx = _guard_and_context()
    await _human_delivery(guard, ctx)
    if terminal in ("new-query", "uninit"):
        ctx.inputs = InvokeInputs(query="original", result={"result_type": "answer", "output": "Image draft"})
        await guard.after_invoke(ctx)
        if terminal == "new-query":
            ctx.inputs = SimpleNamespace(parts=["New work"], source="query")
            await guard.on_user_message(ctx)
        else:
            guard.uninit(guard.owner)
    else:
        ctx.inputs = InvokeInputs(query="original", result={
            "result_type": "answer" if terminal == "exception" else terminal, "output": "Draft",
        })
        if terminal == "exception":
            ctx.exception = RuntimeError("terminal failure")
        await guard.after_invoke(ctx)
    assert guard.take_completed_delivery() is None
    assert guard.owner.system_prompt_builder.get_section("session_input_delivery") is None


@pytest.mark.asyncio
async def test_interrupted_delivery_completes_after_resume_with_actual_latest_reply():
    guard, ctx = _guard_and_context()
    await _human_delivery(guard, ctx)
    ctx.inputs = InvokeInputs(query="original", result={"result_type": "interrupt"})
    await guard.after_invoke(ctx)
    assert guard.take_completed_delivery() is None
    guard.owner.active_round = SimpleNamespace(run_kind="user", work=SimpleNamespace(request_id="resume-id"))
    ctx.inputs = InvokeInputs(query=InteractiveInput())
    await guard.before_invoke(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=3)
    await guard.before_model_call(ctx)
    await _capture(guard, ctx, "Actual resumed image answer")
    ctx.inputs = InvokeInputs(query="original", result={"result_type": "answer", "output": ""})
    await guard.after_invoke(ctx)
    plan = guard.take_completed_delivery()
    assert plan.original_request_id == "original-query"
    assert plan.latest_draft == "Actual resumed image answer" and plan.original_reply == "99 * 99 = 9801"
