# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Independent Bus/Builder tests; native bindings are explicit test setup only."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig, CompactionError
from openjiuwen.core.context_engine.compaction import message_fingerprint
from openjiuwen.core.context_engine.processor.compressor.full_compact_processor import FullCompactProcessorConfig
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext, HandlerLevel
from openjiuwen.core.extension.runtime import Runtime
from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage, ToolMessage, ToolCall
from openjiuwen.core.session.agent import Session

from jiuwenswarm.extensions_v2.session_compaction.dispatch import CompactMessageCodec
from jiuwenswarm.extensions_v2.session_compaction.service import SessionCompactionService
from jiuwenswarm.extensions_v2.event_types.session_compact import (
    SessionCompacted, SessionCompactSkipped, register_session_compact_event,
)
from jiuwenswarm.extensions_v2.handler_ops.session_ops import register_session_ops


async def _runtime(*, bind_native=True):
    bus = ExtensionEventBus()
    register_session_compact_event(bus)
    runtime = Runtime()
    service = SessionCompactionService(bus, invocation_guard=runtime.is_active_invocation)
    register_session_ops(bus, compact_service=service)
    await bus.trigger(Event("ops.build", None))
    engine = ContextEngine(ContextEngineConfig(enable_tiktoken_counter=False, enable_openrouter_model_context_window_tokens=False))
    session = Session(session_id="compact-a")
    session.commit = AsyncMock()
    context = await engine.create_context(session=session, history_messages=[
        UserMessage(content="Keep the API constraints. " * 100),
        AssistantMessage(content="Investigated the project. " * 100),
        UserMessage(content="Tests are pending. " * 100),
        AssistantMessage(content="Latest reply."),
    ], processors=[("FullCompactProcessor", FullCompactProcessorConfig(
        trigger_total_tokens=1, messages_to_keep=1, compression_call_max_tokens=100000,
    ))])
    calls = []
    class Model:
        async def invoke(self, *args, **kwargs):
            calls.append(kwargs.get("messages", args[0] if args else None))
            return AssistantMessage(content="API constraints retained. Tests pending.")
    context._processors[0]._model = Model()
    binding = SessionContext("compact-a", cwd="D:/workspace/project")
    if bind_native:
        service.bind(engine, binding, busy=lambda: False)
    return bus, service, engine, context, binding, calls, runtime


@pytest.mark.asyncio
async def test_default_ops_reports_not_integrated_without_dispatch_or_mutation():
    bus, service, engine, context, binding, calls, runtime = await _runtime(bind_native=False)
    before = message_fingerprint(context.get_messages())
    compact_handler = AsyncMock(side_effect=AssertionError("no native request may be invented"))
    bus.register_event_handler("session.compact", compact_handler, {}, HandlerLevel.USER)
    async def original_core(event, ops, next_handler):
        return "original event completed"
    bus.register_event_type("test.caller", lambda value, pattern: True, original_core)
    async def user(event, ops, next_handler):
        assert callable(ops.session.compact)
        assert ops.session_context.session_id == "compact-a"
        with pytest.raises(CompactionError, match="COMPACT_NOT_INTEGRATED"):
            await ops.session.compact(instructions="Keep pending work")
        return await next_handler(event)
    bus.register_event_handler("test.caller", runtime._tracked_handler("demo", user), {}, HandlerLevel.USER)
    assert await bus.trigger(Event("test.caller", {}), binding) == "original event completed"
    compact_handler.assert_not_awaited()
    assert not service.bindings and not service.tasks and engine._compaction.hook is None
    assert message_fingerprint(context.get_messages()) == before and not calls
    context.get_session_ref().commit.assert_not_awaited()
    await service.close()


@pytest.mark.asyncio
async def test_explicit_test_binding_traces_ops_user_builtin_core_and_returns_result(monkeypatch):
    from jiuwenswarm.extensions_v2.event_handlers import session_compact as handlers
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    trace = []
    prepare = handlers.prepare_compact
    async def observed_core(event, ops):
        trace.append("compact-core")
        assert ops.session_context == binding
        return await prepare(event, ops)
    monkeypatch.setattr(handlers, "prepare_compact", observed_core)
    async def outer_core(event, ops, next_handler):
        trace.append("outer-core")
        return "done"
    bus.register_event_type("test.caller", lambda value, pattern: True, outer_core)
    async def outer(event, ops, next_handler):
        trace.append("outer-user")
        result = await ops.session.compact(instructions="Keep API constraints")
        assert isinstance(result, SessionCompacted)
        assert "Tests pending" in " ".join(message["text"] for message in result.messages)
        trace.append("outer-resumed")
        return await next_handler(event)
    async def user(event, ops, next_handler):
        trace.append("compact-user-before")
        changed = replace(event.event_input, instructions=event.event_input.instructions + "; keep tests")
        result = await next_handler(Event(event.event_type, changed))
        trace.append("compact-user-after")
        return result
    async def builtin(event, ops, next_handler):
        trace.append("compact-builtin-before")
        result = await next_handler(event)
        trace.append("compact-builtin-after")
        return result
    bus.register_event_handler("test.caller", runtime._tracked_handler("demo", outer), {}, HandlerLevel.USER)
    bus.register_event_handler("session.compact", user, {}, HandlerLevel.USER)
    bus.register_event_handler("session.compact", builtin, {}, HandlerLevel.BUILTIN)
    assert await bus.trigger(Event("test.caller", {}), binding) == "done"
    assert trace == ["outer-user", "compact-user-before", "compact-builtin-before", "compact-core",
                     "compact-builtin-after", "compact-user-after", "outer-resumed", "outer-core"]
    assert len(calls) == 1 and "keep tests" in calls[0][0].content
    await service.close()


@pytest.mark.asyncio
async def test_native_input_start_does_not_bind_compaction_engine(monkeypatch):
    from jiuwenswarm.extensions_v2 import assembly
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    manager = SimpleNamespace(ensure_session_started=AsyncMock())
    monkeypatch.setattr(assembly, "get_extension_manager", lambda: manager)
    bind = AsyncMock(side_effect=AssertionError("native input must not install the compact bridge"))
    adapter = SimpleNamespace(_project_dir=None, _bind_extension_compaction=bind)
    request = SimpleNamespace(req_method=interface_deep.ReqMethod.CHAT_SEND,
                              session_id="s1", request_id="r1", channel_id="web")
    await interface_deep.JiuWenSwarmDeepAdapter._ensure_extension_session_started(adapter, request, {}, None)
    bind.assert_not_called()
    manager.ensure_session_started.assert_awaited_once()
    assert manager.ensure_session_started.call_args.args[0].session_id == "s1"


@pytest.mark.asyncio
async def test_ops_to_event_to_native_result_and_next_original_event():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    trace = []
    captured = []
    async def original_core(event, ops, next_handler):
        trace.append("original-core")
        return "done"
    bus.register_event_type("test.compact", lambda value, pattern: True, original_core)
    async def user(event, ops, next_handler):
        trace.append("caller")
        result = await ops.session.compact(instructions="Keep order module tasks")
        assert isinstance(result, SessionCompacted)
        assert all("handle" not in message for message in result.messages)
        captured.append((ops, result))
        return await next_handler(event)
    async def compact_user(event, ops, next_handler):
        assert ops.session_context.session_id == "compact-a"
        trace.append(event.event_input.trigger)
        value = replace(event.event_input, instructions=event.event_input.instructions + "; keep tests")
        return await next_handler(Event(event.event_type, value))
    bus.register_event_handler("test.compact", runtime._tracked_handler("demo", user), {}, HandlerLevel.USER)
    bus.register_event_handler("session.compact", compact_user, {}, HandlerLevel.USER)
    assert await bus.trigger(Event("test.compact", {}), binding) == "done"
    assert trace == ["caller", "plugin", "original-core"]
    assert len(calls) == 1 and "keep tests" in calls[0][0].content
    assert not runtime.is_active_invocation(captured[0][0])
    context.get_session_ref().commit.assert_awaited_once()
    assert "Tests pending" in " ".join(message.content for message in context.get_messages())
    await service.close()


@pytest.mark.asyncio
async def test_post_next_skip_preserves_live_context_and_durable_state():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    before = message_fingerprint(context.get_messages())
    async def veto(event, ops, next_handler):
        result = await next_handler(event)
        assert isinstance(result, SessionCompacted)
        assert message_fingerprint(context.get_messages()) == before
        return SessionCompactSkipped("keep diagnostics")
    bus.register_event_handler("session.compact", veto, {}, HandlerLevel.USER)
    result = await service.run("compact-a")
    assert result.skip == "keep diagnostics"
    assert message_fingerprint(context.get_messages()) == before
    context.get_session_ref().commit.assert_not_awaited()
    await service.close()


@pytest.mark.asyncio
async def test_user_can_supply_summary_without_running_default_model():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    async def custom(event, ops, next_handler):
        return SessionCompacted([{"role": "user", "text": "My summary", "toolUses": []}])
    bus.register_event_handler("session.compact", custom, {}, HandlerLevel.USER)
    result = await service.run("compact-a", trigger="plugin")
    assert result.messages[0]["text"] == "My summary"
    assert not calls
    assert context.get_messages()[0].content == "My summary"
    await service.close()


@pytest.mark.asyncio
async def test_direct_trigger_cannot_invent_platform_compaction():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    with pytest.raises(Exception):
        await bus.trigger(Event("session.compact", {}), binding)
    assert not calls
    await service.close()


@pytest.mark.asyncio
async def test_busy_and_missing_session_do_not_enter_event():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    service.bindings["compact-a"].busy = lambda: True
    with pytest.raises(CompactionError, match="COMPACT_BUSY"):
        await service.run("compact-a")
    with pytest.raises(CompactionError, match="COMPACT_SESSION_UNAVAILABLE"):
        await service.run("other-session")
    assert not calls
    await service.close()


def test_handle_keeps_complete_native_message_while_removal_constructs_new_text():
    codec = CompactMessageCodec()
    native = UserMessage(content=[{"type": "image", "data": "opaque-media"}], metadata={"private": "keep"})
    view = codec.encode([native])[0]
    view["text"] = "edited"
    decoded = codec.decode([view])[0]
    assert decoded == native and decoded is not native
    view.pop("handle")
    assert codec.decode([view])[0].content == "edited"


def test_unknown_repeated_or_cross_invocation_handles_are_rejected():
    codec = CompactMessageCodec()
    view = codec.encode([UserMessage(content="one")])[0]
    with pytest.raises(CompactionError, match="COMPACT_INVALID_RESULT"):
        codec.decode([view, view])
    with pytest.raises(CompactionError, match="COMPACT_INVALID_RESULT"):
        CompactMessageCodec().decode([view])


def test_tool_message_roundtrip_preserves_native_type_and_call_identity():
    codec = CompactMessageCodec()
    original = [AssistantMessage(content="", tool_calls=[ToolCall(id="call-1", name="read_file", type="function", arguments='{"path":"src/a.py"}')]),
                ToolMessage(content="original result", tool_call_id="call-1", metadata={"private": True})]
    views = codec.encode(original)
    assert views[1]["role"] == "user" and views[1]["toolResults"][0]["tool_use_id"] == "call-1"
    assert list(codec.decode(views)) == original


@pytest.mark.asyncio
async def test_precompute_returns_candidate_but_does_not_install_or_commit():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    before = message_fingerprint(context.get_messages())
    candidate = await service.run("compact-a", trigger="precompute")
    assert candidate.tokens_after is None
    assert message_fingerprint(context.get_messages()) == before
    context.get_session_ref().commit.assert_not_awaited()
    await service.run("compact-a", trigger="manual")
    assert len(calls) == 1
    await service.close()


@pytest.mark.asyncio
async def test_opt_in_precompute_waits_for_input_task_exit_and_deduplicates():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    before = message_fingerprint(context.get_messages())
    async def completed_input():
        service.schedule_precompute("compact-a", enabled=True, ratio=0.000001)
        service.schedule_precompute("compact-a", enabled=True, ratio=0.000001)
        assert not calls
    await asyncio.create_task(completed_input())
    for _ in range(100):
        await asyncio.sleep(0)
        if engine._compaction.cache and not service.tasks:
            break
    assert engine._compaction.cache and len(calls) == 1
    assert message_fingerprint(context.get_messages()) == before
    context.get_session_ref().commit.assert_not_awaited()
    await service.close()


@pytest.mark.asyncio
async def test_reentrant_ops_does_not_retrigger_event_or_skip_other_policies():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    trace = []
    results = []
    async def core(event, ops, next_handler):
        return "done"
    bus.register_event_type("test.caller", lambda value, pattern: True, core)
    async def caller(event, ops, next_handler):
        results.append(await ops.session.compact())
        return await next_handler(event)
    async def first(event, ops, next_handler):
        trace.append("first")
        with pytest.raises(CompactionError, match="COMPACT_BUSY|COMPACT_REENTRANT"):
            await ops.session.compact()
        return await next_handler(event)
    async def second(event, ops, next_handler):
        trace.append("second")
        return await next_handler(event)
    for event_type, handler in [("test.caller", caller), ("session.compact", first), ("session.compact", second)]:
        bus.register_event_handler(event_type, runtime._tracked_handler("demo", handler), {}, HandlerLevel.USER)
    assert await bus.trigger(Event("test.caller", {}), binding) == "done"
    assert trace == ["first", "second"] and len(calls) == 1
    assert isinstance(results[0], SessionCompacted)
    await service.close()


@pytest.mark.asyncio
async def test_plugin_ops_rejects_child_task_expired_and_unmanaged_invocations():
    bus, service, engine, context, binding, calls, runtime = await _runtime()
    captured = []
    async def core(event, ops, next_handler):
        return "done"
    bus.register_event_type("test.caller", lambda value, pattern: True, core)
    async def user(event, ops, next_handler):
        captured.append(ops)
        with pytest.raises(CompactionError, match="COMPACT_UNBOUND_INVOCATION"):
            await asyncio.create_task(ops.session.compact())
        return await next_handler(event)
    handler_id = bus.register_event_handler("test.caller", runtime._tracked_handler("demo", user), {}, HandlerLevel.USER)
    assert await bus.trigger(Event("test.caller", {}), binding) == "done"
    with pytest.raises(CompactionError, match="COMPACT_UNBOUND_INVOCATION"):
        await captured[0].session.compact()
    bus.unregister_event_handler(handler_id)
    async def unmanaged(event, ops, next_handler):
        captured.append(ops)
        with pytest.raises(CompactionError, match="COMPACT_UNBOUND_INVOCATION"):
            await ops.session.compact()
        return await next_handler(event)
    bus.register_event_handler("test.caller", unmanaged, {}, HandlerLevel.USER)
    assert await bus.trigger(Event("test.caller", {}), binding) == "done"
    with pytest.raises(CompactionError, match="COMPACT_UNBOUND_INVOCATION"):
        await captured[-1].session.compact()
    assert not calls
    context.get_session_ref().commit.assert_not_awaited()
    await service.close()
