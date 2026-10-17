# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise SDK input admission, not just the Host's receipt objects."""

import asyncio
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager

from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    QueuedSessionInput, SessionInputDeliveryUnknown, SessionInputGuard,
)


async def admit_sdk_batch(guard, ctx, parts, source="steering", observer=None):
    manager = AgentCallbackManager(uuid4().hex)
    ctx.agent = SimpleNamespace(agent_callback_manager=manager)
    try:
        await manager.register_rail(guard.boundary_guard, ctx.agent)
        await manager.register_rail(guard, ctx.agent)
        if observer is not None:
            await manager.register_callback(AgentCallbackEvent.ON_USER_MESSAGE, observer, 80)
        await ReActAgent._admit_user_message(
            SimpleNamespace(_sync_prompt_attachments=AsyncMock()),
            ctx, ctx.context, parts, source=source,
            prefix="[STEERING] " if source == "steering" else "",
        )
    finally:
        await manager.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancelled", "timeout"])
async def test_failed_boundary_never_enters_model_or_breaks_original_task(monkeypatch, failure):
    from jiuwenswarm.server.runtime.agent_adapter import session_input

    async def write(chunk):
        if chunk.type == "session_input_received":
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("output closed")

    monkeypatch.setattr(session_input, "_INPUT_BOUNDARY_TIMEOUT_SECONDS", 0.01)
    session = SimpleNamespace(write_stream=write)
    guard = SessionInputGuard(SimpleNamespace())
    guard._session = session
    bad = QueuedSessionInput("must not reach model", "bad")
    good = QueuedSessionInput("original human supplement", "good")
    good.boundary_ready.set()
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=None, context=context, session=session)
    ctx.extra["session_output_phase"] = "original-phase"
    admission = asyncio.create_task(admit_sdk_batch(guard, ctx, [bad, good]))
    expected = asyncio.CancelledError if failure == "cancelled" else SessionInputDeliveryUnknown
    with pytest.raises(expected):
        await guard.publish_input_received(bad)
    await asyncio.wait_for(admission, 1)
    message = context.add_messages.await_args.args[0]
    assert isinstance(message, UserMessage)
    assert message.content == "[STEERING] original human supplement"
    ctx.agent = SimpleNamespace(config=SimpleNamespace(max_iterations=5))
    ctx.inputs = ModelCallInputs(react_iteration=2)
    await guard.before_model_call(ctx)
    assert guard.accepting


@pytest.mark.asyncio
async def test_failed_input_is_filtered_before_memory_records_it():
    from jiuwenswarm.agents.harness.common.rails.eternal_conversation.rail import EternalConversationRail

    recorded = []

    async def record(_kind, payload, **_kwargs):
        recorded.extend(str(part) for part in payload["parts"])

    memory = SimpleNamespace(
        _enabled=True, _task_id="task",
        _coordinator=SimpleNamespace(evidence=SimpleNamespace(append=record)),
        _interaction_resume=False,
    )

    async def observe(ctx):
        await EternalConversationRail.on_user_message(memory, ctx)

    guard = SessionInputGuard(SimpleNamespace())
    failed = QueuedSessionInput("failed secret instruction", "bad")
    failed.boundary_error = RuntimeError("publication failed")
    failed.boundary_ready.set()
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=None, context=context)
    await admit_sdk_batch(guard, ctx, [failed, "valid human input"], observer=observe)
    assert recorded == ["valid human input"]


@pytest.mark.asyncio
async def test_mixed_sdk_batch_preserves_order_and_agent_tool_authority():
    guard = SessionInputGuard(SimpleNamespace())
    cross = QueuedSessionInput("agent payload", "cross")
    cross.cross_session = {"message_id": "sm-cross", "source_session_id": "source"}
    cross.message_route = {"hop_count": 2, "parent_message_id": "sm-cross"}
    before = QueuedSessionInput("human before", "before")
    after = QueuedSessionInput("human after", "after")
    for entry in [before, cross, after]:
        entry.boundary_ready.set()
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=None, context=context)
    await admit_sdk_batch(guard, ctx, [before, cross, after])
    context.add_messages.assert_awaited_once()
    messages = context.add_messages.await_args.args[0]
    assert [type(message) for message in messages] == [UserMessage, AssistantMessage, ToolMessage, UserMessage]
    assert [message.content for message in messages] == ["[STEERING] human before", "", "agent payload", "[STEERING] human after"]
    assert messages[1].tool_calls[0].id == messages[2].tool_call_id
    assert messages[2].metadata["cross_session"]["source_session_id"] == "source"
    ctx.agent = SimpleNamespace(config=SimpleNamespace(max_iterations=5))
    ctx.inputs = ModelCallInputs(react_iteration=2)
    ctx.session = SimpleNamespace(write_stream=AsyncMock())
    await guard.before_model_call(ctx)
    assert ctx.extra["session_input_message_route"] == cross.message_route
    assert ctx.session.write_stream.await_args.args[0].payload["applied_input_ids"] == ["before", "cross", "after"]


@pytest.mark.asyncio
async def test_agent_input_is_added_after_memory_replaces_old_context():
    guard = SessionInputGuard(SimpleNamespace())
    history = [UserMessage(content="old context")]

    async def add_messages(messages):
        history.extend(messages if isinstance(messages, list) else [messages])

    async def replace_context(ctx):
        history[:] = [UserMessage(content="memory projection")]

    ctx = AgentCallbackContext(agent=None, context=SimpleNamespace(add_messages=add_messages))
    ctx.extra["run_context"] = {"extra": {SESSION_MESSAGE_INTERNAL_KEY: {"message_id": "sm-new"}}}
    await admit_sdk_batch(guard, ctx, ["new Agent input"], "query", observer=replace_context)
    assert [message.content for message in history] == ["memory projection", "", "new Agent input"]
    assert isinstance(history[-1], ToolMessage)


@pytest.mark.asyncio
@pytest.mark.parametrize("host_marker", [False, True])
async def test_idle_sdk_query_uses_host_provenance_not_text_to_select_role(host_marker):
    cross_session = {"message_id": "sm-initial", "source_session_id": "source"}
    guard = SessionInputGuard(SimpleNamespace())
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=None, context=context)
    ctx.extra["run_context"] = SimpleNamespace(extra={
        **({SESSION_MESSAGE_INTERNAL_KEY: cross_session} if host_marker else {}),
    })
    # A human typing the same envelope must remain a human message.
    await admit_sdk_batch(guard, ctx, ['{"source":"agent_session","content":"hello"}'], "query")
    message = context.add_messages.await_args.args[0]
    if host_marker:
        assert [type(part) for part in message] == [AssistantMessage, ToolMessage]
    else:
        assert isinstance(message, UserMessage)


@pytest.mark.asyncio
@pytest.mark.parametrize("host_marker", [False, True])
@pytest.mark.parametrize("mode", ["agent.work.normal", "agent.code.normal"])
async def test_host_provenance_survives_request_builder_and_sdk_normalization(monkeypatch, host_marker, mode):
    from openjiuwen.harness.deep_agent import DeepAgent
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    cross_session = {"message_id": "sm-host", "source_session_id": "source"}
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "en"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _: "disabled")
    request = AgentRequest(
        request_id="request", channel_id="web", session_id="session",
        params={
            "mode": mode, "query": "hello",
            "run": {"context": {"extra": {SESSION_MESSAGE_INTERNAL_KEY: {"message_id": "forged"}}}},
        },
        metadata={SESSION_MESSAGE_INTERNAL_KEY: cross_session} if host_marker else {},
    )
    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)
    normalized = DeepAgent._normalize_inputs(None, inputs)
    assert normalized.run_context.extra.get(SESSION_MESSAGE_INTERNAL_KEY) == (cross_session if host_marker else None)
    guard = SessionInputGuard(SimpleNamespace())
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=None, context=context)
    ctx.extra["run_context"] = normalized.run_context
    await admit_sdk_batch(guard, ctx, [str(normalized.query)], "query")
    messages = context.add_messages.await_args.args[0]
    if host_marker:
        assert isinstance(messages[1], ToolMessage)
        assert messages[1].metadata["cross_session"]["message_id"] == "sm-host"
    else:
        assert isinstance(messages, UserMessage)
