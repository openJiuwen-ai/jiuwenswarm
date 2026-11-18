# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Message queue ownership and delivery failure regression coverage."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from e2a.message import Message, ReqMethod

from agent_gateway import telemetry
from agent_gateway.channel_manager.channel_manager import ChannelManager
from agent_gateway.message_handler.message_handler import MessageHandler


def message(request_id="request", channel="web", kind="event"):
    return Message(id=request_id, type=kind, channel_id=channel, session_id="session",
                   params={}, timestamp=0, ok=True, payload={})


def operation(monkeypatch):
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    return telemetry.start_request_trace("request", "chat.send", channel="web", connection_id="connection")


@pytest.mark.asyncio
@pytest.mark.parametrize("channel, method", [
    ("tui", ReqMethod.CHAT_SEND),
    ("feishu", ReqMethod.HISTORY_GET),
])
async def test_non_web_user_queue_starts_and_reuses_request_trace(monkeypatch, channel, method):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=telemetry.create_request_id_generator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_runtime", telemetry.GatewayTelemetry(tracer=provider.get_tracer("test")))
    handler = object.__new__(MessageHandler)
    handler._user_messages = asyncio.Queue()
    msg = Message(
        id="request", type="req", channel_id=channel, session_id="session", params={},
        timestamp=0, ok=True, req_method=method, metadata={"ws_id": "connection"},
    )
    try:
        handler.publish_user_messages_nowait(msg)
        op = telemetry.get_message_trace(msg)
        assert op is not None and op.accepted and not op.finished
        assert op.channel == channel and op.method == method.value
        assert op.connection_id == "connection"
        handler.publish_user_messages_nowait(msg)
        assert telemetry.get_message_trace(msg) is op
        assert await handler.consume_user_messages() is msg
        assert await handler.consume_user_messages() is msg

        env = SimpleNamespace(request_id=msg.id, channel=channel, method=method.value)
        with telemetry.bind_message_trace(msg):
            with telemetry.bind_agent_request_trace(env) as owner:
                assert owner is op
                with provider.get_tracer("downstream").start_as_current_span("agent"):
                    pass
        assert not op.finished
        telemetry.complete_request_trace(msg)
        assert op.finished
        assert telemetry.queue_request_trace(msg) is None
        finished = exporter.get_finished_spans()
        roots = [span for span in finished if span.name == "gw.session.message"]
        assert len(roots) == 1
        child = next(span for span in finished if span.name == "agent")
        assert child.context.trace_id == roots[0].context.trace_id
        assert child.parent.span_id == roots[0].context.span_id
        assert telemetry.current_request_trace() is None
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, kind, method", [
    (True, "event", ReqMethod.CHAT_SEND),
    (True, "req", None),
    (False, "req", ReqMethod.CHAT_SEND),
])
async def test_non_web_queue_does_not_start_unrelated_or_disabled_trace(monkeypatch, enabled, kind, method):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=telemetry.create_request_id_generator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test") if enabled else None
    monkeypatch.setattr(telemetry, "_runtime", telemetry.GatewayTelemetry(tracer=tracer))
    handler = object.__new__(MessageHandler)
    handler._user_messages = asyncio.Queue()
    msg = Message(
        id="request", type=kind, channel_id="tui", session_id="session", params={},
        timestamp=0, ok=True, req_method=method,
    )
    try:
        handler.publish_user_messages_nowait(msg)
        assert await handler.consume_user_messages() is msg
        assert telemetry.get_message_trace(msg) is None
        assert not exporter.get_finished_spans()
        assert telemetry.current_request_trace() is None
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_response_queue_holds_operation_until_dispatch_drops_missing_channel(monkeypatch):
    op = operation(monkeypatch)
    handler = SimpleNamespace(resolve_app_id=lambda msg: "")
    queue = asyncio.Queue()
    async def consume(timeout):
        return await queue.get()
    handler.consume_robot_messages = consume
    manager = ChannelManager(handler, {})
    manager._running = True
    manager._inject_file_delivery_fanout = AsyncMock()
    with telemetry.bind_request_trace(op):
        queued = telemetry.queue_response_trace(message())
    await queue.put(queued)
    op.complete("success")
    assert not op.finished
    task = asyncio.create_task(manager._dispatch_robot_messages())
    try:
        async with asyncio.timeout(2):
            while not op.finished:
                await asyncio.sleep(0)
        assert op.finished
        assert op.outcome == "incomplete"
        assert op.reason == "no_channel"
    finally:
        manager._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_rejected_response_queue_releases_reference(monkeypatch):
    op = operation(monkeypatch)
    handler = object.__new__(MessageHandler)
    handler._robot_messages = asyncio.Queue(maxsize=1)
    handler._robot_messages.put_nowait(message("other"))
    with telemetry.bind_request_trace(op):
        with pytest.raises(asyncio.QueueFull):
            handler.publish_robot_messages_nowait(message())
    op.complete("success")
    assert op.finished
    assert op.outcome == "incomplete"
    assert op.reason == "queue_failed"


@pytest.mark.asyncio
async def test_non_stream_failure_is_not_overwritten_by_outer_success(monkeypatch):
    op = operation(monkeypatch)
    handler = object.__new__(MessageHandler)
    handler._send_non_stream_agent_request = AsyncMock(side_effect=RuntimeError("failed"))
    handler._build_error_out_message = lambda msg, exc: message()
    handler.publish_robot_messages = AsyncMock()
    handler._notify_chat_completed = lambda *args, **kwargs: None
    handler._stream_user_ids = {}
    msg = message(kind="req")
    telemetry.attach_message_trace(msg, op)
    env = SimpleNamespace(request_id="request")
    assert await handler._process_non_stream_request(msg, env) is None
    assert not op.finished
    op.complete("success")
    assert op.finished
    assert op.outcome == "failed"



@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_background_interrupt_retains_parent_until_task_exits(monkeypatch, cancel):
    op = operation(monkeypatch)
    handler = object.__new__(MessageHandler)
    gate = asyncio.Event()
    async def send(*args, **kwargs):
        await gate.wait()

    handler._send_interrupt_to_agent = send
    env = SimpleNamespace(channel="web", request_id="interrupt", method="chat.cancel")
    with telemetry.bind_request_trace(op):
        task = handler._start_interrupt_task(env)
    op.complete("success")
    assert not op.finished
    if cancel:
        task.cancel()
    else:
        gate.set()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert op.finished
    assert op.outcome == ("incomplete" if cancel else "success")


@pytest.mark.asyncio
async def test_stream_cleanup_failure_overrides_earlier_cancellation(monkeypatch):
    op = operation(monkeypatch)
    handler = object.__new__(MessageHandler)
    env = SimpleNamespace(channel="web", request_id="request", method="chat.send",
                          _gateway_trace_operation=op)

    async def process(*args, **kwargs):
        env._gateway_trace_outcome = "cancelled"
        raise RuntimeError("cleanup failed")

    handler._process_stream_impl = process
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await handler.process_stream(env, "session", None)
    assert op.finished
    assert op.outcome == "failed"


@pytest.mark.asyncio
async def test_background_notice_retains_parent_until_enqueued(monkeypatch):
    op = operation(monkeypatch)
    handler = object.__new__(MessageHandler)
    gate = asyncio.Event()
    queued = asyncio.Event()
    async def notice(*args):
        await gate.wait()
        queued.set()
    handler.send_channel_notice = notice
    msg = message(kind="req")
    telemetry.attach_message_trace(msg, op)
    with telemetry.bind_request_trace(op):
        handler._start_traced_notice(msg, "invalid command")
    op.complete("success")
    assert not op.finished
    gate.set()
    await asyncio.wait_for(queued.wait(), 1)
    async with asyncio.timeout(1):
        while not op.finished:
            await asyncio.sleep(0)
    assert op.outcome == "success"


@pytest.mark.asyncio
async def test_shutdown_releases_queued_traces_without_changing_messages(monkeypatch):
    request_op = operation(monkeypatch)
    response_op = telemetry.start_request_trace("response", "chat.send", channel="web", connection_id="c")
    late_op = telemetry.start_request_trace("late", "chat.send", channel="web", connection_id="c")
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._user_messages = asyncio.Queue(maxsize=3)
    handler._robot_messages = asyncio.Queue(maxsize=4)
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)

    first = message(kind="req")
    first.metadata = {"identity": "retained"}
    with telemetry.bind_request_trace(request_op):
        await handler.publish_user_messages(first)
    second = message("plain", kind="req")
    handler._user_messages.put_nowait(second)
    with telemetry.bind_request_trace(response_op):
        handler.publish_robot_messages_nowait(message("response"))
    response_op.complete("success")
    assert not response_op.finished
    before_user = list(handler._user_messages._queue)
    before_robot = list(handler._robot_messages._queue)
    user_unfinished = handler._user_messages._unfinished_tasks
    robot_unfinished = handler._robot_messages._unfinished_tasks

    await manager.stop_dispatch()
    assert response_op.finished and response_op.outcome == "incomplete"
    assert response_op.reason == "shutdown"
    assert list(handler._robot_messages._queue) == before_robot
    assert handler._robot_messages._unfinished_tasks == robot_unfinished
    assert not hasattr(before_robot[0], "_gateway_trace_delivery")
    assert not hasattr(before_robot[0], "_gateway_trace_operation")

    # A producer can enqueue its last frame after ChannelManager has stopped.
    with telemetry.bind_request_trace(late_op):
        handler.publish_robot_messages_nowait(message("late"))
    late_op.complete("success")
    late_message = handler._robot_messages._queue[-1]
    await handler.stop_forwarding()
    assert request_op.finished and request_op.outcome == "incomplete"
    assert request_op.reason == "shutdown"
    assert late_op.finished and late_op.outcome == "incomplete"
    assert late_op.reason == "shutdown"
    assert list(handler._user_messages._queue) == before_user
    assert list(handler._robot_messages._queue) == before_robot + [late_message]
    assert handler._user_messages._unfinished_tasks == user_unfinished
    assert handler._robot_messages._unfinished_tasks == robot_unfinished + 1
    assert handler._user_messages.maxsize == 3 and handler._robot_messages.maxsize == 4
    assert first.metadata == {"identity": "retained"}
    assert not hasattr(first, "_gateway_trace_operation")
    assert not hasattr(first, "_gateway_trace_enqueued_at")
    assert not hasattr(late_message, "_gateway_trace_delivery")


@pytest.mark.asyncio
async def test_stop_dispatch_accepts_handler_without_robot_queue():
    manager = ChannelManager(SimpleNamespace(), {})
    await manager.stop_dispatch()


@pytest.mark.asyncio
async def test_handler_stop_keeps_response_trace_until_active_manager_stops(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)
    manager._running = True
    with telemetry.bind_request_trace(op):
        handler.publish_robot_messages_nowait(message())
    op.complete("success")
    await handler.stop_forwarding()
    assert not op.finished
    assert hasattr(handler._robot_messages._queue[0], "_gateway_trace_delivery")
    await manager.stop_dispatch()
    assert op.finished and op.outcome == "incomplete"
    assert op.reason == "shutdown"


@pytest.mark.asyncio
async def test_response_from_late_background_task_has_no_shutdown_hold(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)
    await manager.stop_dispatch()
    await handler.stop_forwarding()
    with telemetry.bind_request_trace(op):
        await handler.publish_robot_messages(message())
    op.complete("success")
    assert op.finished and op.outcome == "incomplete"
    assert op.reason == "shutdown"
    assert handler._robot_messages.qsize() == 1
    queued = handler._robot_messages.get_nowait()
    assert queued.id == "request"
    assert not hasattr(queued, "_gateway_trace_operation")
    assert not hasattr(queued, "_gateway_trace_delivery")


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_request_after_stop_remains_queued_without_trace_hold(monkeypatch, sync):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._user_messages = asyncio.Queue(maxsize=2)
    await handler.stop_forwarding()
    earlier = message("earlier", kind="req")
    handler._user_messages.put_nowait(earlier)
    later = message(kind="req")
    later.metadata = {"business": "kept"}
    with telemetry.bind_request_trace(op):
        if sync:
            handler.publish_user_messages_nowait(later)
        else:
            await handler.publish_user_messages(later)
    assert op.finished and op.outcome == "incomplete"
    assert op.reason == "shutdown"
    assert list(handler._user_messages._queue) == [earlier, later]
    assert handler._user_messages._unfinished_tasks == 2
    assert handler._user_messages.full()
    assert later.metadata == {"business": "kept"}
    assert not hasattr(later, "_gateway_trace_operation")
    assert not hasattr(later, "_gateway_trace_enqueued_at")


@pytest.mark.asyncio
async def test_restart_restores_queue_trace_admission(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    manager = ChannelManager(handler, {})
    handler.set_channel_manager(manager)
    await manager.stop_dispatch()
    await handler.stop_forwarding()
    await manager.start_dispatch()
    await handler.start_forwarding()
    request = message(kind="req")
    with telemetry.bind_request_trace(op):
        handler.publish_user_messages_nowait(request)
    assert op.accepted and not op.finished
    assert telemetry.get_message_trace(request) is op
    await handler.stop_forwarding()
    await manager.stop_dispatch()
    assert op.finished and op.outcome == "incomplete"


@pytest.mark.asyncio
async def test_preprocess_stages_report_duration_without_changing_failures(monkeypatch):
    op = operation(monkeypatch)
    handler = object.__new__(MessageHandler)
    msg = message(kind="req")
    handler._apply_session_login_owner = AsyncMock(side_effect=RuntimeError("owner unavailable"))
    env = SimpleNamespace(request_id="request", channel="web", method="chat.send")
    with telemetry.bind_request_trace(op):
        assert await handler._prepare_agent_dispatch_message(msg) is msg
        with pytest.raises(RuntimeError, match="owner unavailable"):
            await handler._send_non_stream_agent_request(env)
    events = {event.name: event for event in op.span.events}
    assert events["preprocess.session"].attributes["duration_ms"] >= 0
    assert events["preprocess.session"].attributes["outcome"] == "success"
    assert events["preprocess.login_owner"].attributes["duration_ms"] >= 0
    assert events["preprocess.login_owner"].attributes["outcome"] == "failed"
    assert op.outcome == "success"
    op.complete()


@pytest.mark.asyncio
async def test_unary_lock_wait_event_is_recorded_before_request_execution(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._broadcast_task_global_running = AsyncMock()
    request = message(kind="req")
    telemetry.attach_message_trace(request, op)
    lock = asyncio.Lock()
    await lock.acquire()
    handler._non_stream_chat_locks[request.session_id] = lock
    entered = asyncio.Event()
    gate = asyncio.Event()
    async def process(*args, **kwargs):
        entered.set()
        await gate.wait()
    handler._process_non_stream_request = process
    await handler._start_non_stream_chat_task(request, SimpleNamespace(request_id="request"))
    task = handler._non_stream_chat_tasks["request"]
    await asyncio.sleep(0)
    assert not entered.is_set()
    lock.release()
    await asyncio.wait_for(entered.wait(), 1)
    event = next(event for event in op.span.events if event.name == "queue.lock_acquired")
    assert event.attributes["queue_wait_ms"] >= 0
    assert not task.done()
    gate.set()
    await task
    op.complete()


@pytest.mark.asyncio
async def test_cancelled_tool_results_retain_explicit_request_owner(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    env = SimpleNamespace(request_id="derived-cancel", channel="web", method="chat.cancel")
    with telemetry.bind_request_trace(op):
        with telemetry.bind_agent_request_trace(env) as child:
            await handler._send_cancelled_tool_results("web", "session", {
                "cancelled_tools": [{"tool_name": "search", "tool_call_id": "tool-1"}],
            }, None)
        assert child.finished and telemetry.current_request_trace() is op
    result = await handler.consume_robot_messages(timeout=None)
    assert result.id.startswith("tool_result_")
    assert telemetry.get_message_trace(result) is op
    op.complete()
    assert not op.finished
    telemetry.release_response_trace(result)
    assert op.finished


@pytest.mark.asyncio
async def test_same_request_id_other_message_does_not_claim_background_task(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._is_session_input_message = lambda msg: False
    handler._handle_channel_control = AsyncMock(return_value=True)
    earlier = message(kind="req")
    later = message(kind="req")
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    handler._non_stream_chat_tasks["request"] = task
    handler._non_stream_chat_messages["request"] = earlier
    await handler.start_forwarding()
    with telemetry.bind_request_trace(op):
        await handler.publish_user_messages(later)
    async with asyncio.timeout(2):
        while not handler._handle_channel_control.called:
            await asyncio.sleep(0)
    assert op.finished and op.producer_done
    assert not task.done()
    await handler.stop_forwarding()


@pytest.mark.asyncio
async def test_supplement_stream_keeps_parent_until_producer_and_responses_finish(monkeypatch):
    from e2a.agent_models import AgentResponseChunk
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    entered = asyncio.Event()
    gate = asyncio.Event()
    seen = {}

    class Client:
        async def send_request_stream(self, env):
            child = telemetry.current_request_trace()
            seen["child"] = child
            seen["request_id"] = env.request_id
            entered.set()
            yield AgentResponseChunk(request_id=env.request_id, channel_id="web",
                                     payload={"event_type": "chat.delta", "content": "work"})
            await gate.wait()
            yield AgentResponseChunk(request_id=env.request_id, channel_id="web",
                                     payload=None, is_complete=True)

    handler = MessageHandler(Client())
    handler._sync_agentos_cron_jobs = AsyncMock()
    handler._apply_session_login_owner = AsyncMock()
    handler._broadcast_task_global_running = AsyncMock()
    handler._trigger_before_chat_request_hook = AsyncMock()
    await handler.start_forwarding()
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    with telemetry.bind_request_trace(op):
        handler._enqueue_supplement_continuation(continuation, source)
    op.complete()
    assert not op.finished
    await asyncio.wait_for(entered.wait(), 5)
    child = seen["child"]
    assert child.parent_operation is op
    assert child.request_id == continuation.id and child.trace_id == op.trace_id
    assert not op.finished
    gate.set()
    async with asyncio.timeout(5):
        while handler._stream_tasks:
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    queued = list(handler._robot_messages._queue)
    assert queued and all(telemetry.get_message_trace(item) is op for item in queued)
    assert any(item.id == seen["request_id"] for item in queued)
    assert not op.finished

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from agent_gateway.routing.keys import AgentRef, RoutingKey

    class Socket:
        closed = False
        remote_address = ("127.0.0.1", 12345)
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
        async def send(self, data):
            self.entered.set()
            await self.release.wait()

    socket = Socket()
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._resolve_connection_user_id({"user_id": "alice"}, socket)
    await channel.register_ws(socket, RoutingKey(
        user_id="alice", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="session",
    ))
    op.connection_id = str(id(socket))
    channel._track_request_trace(socket, op)
    try:
        for item in queued:
            await channel.send(item)
            telemetry.release_response_trace(item)
        await asyncio.wait_for(socket.entered.wait(), 5)
        assert not op.finished
        socket.release.set()
        async with asyncio.timeout(5):
            while not op.finished:
                await asyncio.sleep(0)
        assert op.outcome == "success" and op.written_frames > 0
        assert telemetry.current_request_trace() is None
    finally:
        socket.release.set()
        await channel._shutdown_all_writers()
        await handler.stop_forwarding()


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [False, True])
async def test_derived_response_preserves_explicit_owner(monkeypatch, finished):
    op = operation(monkeypatch)
    other = telemetry.start_request_trace("other", "chat.send", channel="web", connection_id="c")
    env = SimpleNamespace(request_id="derived", channel="web", method="chat.send")
    msg = message("derived")
    telemetry.attach_message_trace(msg, other)
    if finished:
        other.complete()
    with telemetry.bind_request_trace(op):
        with telemetry.bind_agent_request_trace(env):
            MessageHandler._attach_derived_response_owner(msg)
    assert msg._gateway_trace_operation is other
    op.complete()
    other.complete()


@pytest.mark.asyncio
async def test_unconsumed_supplement_shutdown_releases_hold_without_removing_message(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    with telemetry.bind_request_trace(op):
        handler._enqueue_supplement_continuation(continuation, source)
    op.complete()
    assert not op.finished
    await handler.stop_forwarding()
    assert op.finished and op.outcome == "incomplete" and op.reason == "shutdown"
    assert list(handler._user_messages._queue) == [continuation]
    assert not hasattr(continuation, "_gateway_trace_continuation")
    telemetry.release_request_continuation(continuation)
    assert op.pending_deliveries == 0


@pytest.mark.asyncio
async def test_supplement_cancelled_before_stream_task_starts_releases_hold(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._should_emit_processing_status_for_stream = lambda msg: False
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    telemetry.retain_request_continuation(continuation, op)
    env = SimpleNamespace(request_id=continuation.id, channel="web", method="chat.send",
                          user_id="", params={})
    await handler._start_stream_task(continuation, env, continuation.id)
    task = handler._stream_tasks[continuation.id]
    task.cancel()
    op.complete()
    assert not op.finished
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert op.finished and op.outcome == "cancelled"
    telemetry.release_request_continuation(continuation)
    assert op.pending_deliveries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_supplement_stream_exit_does_not_complete_original_producer(monkeypatch, cancel):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._should_emit_processing_status_for_stream = lambda msg: False
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    telemetry.retain_request_continuation(continuation, op)
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def process(env, session_id, metadata, *, emit_processing_status):
        response = message(env.request_id)
        telemetry.attach_message_trace(response, op)
        await handler.publish_robot_messages(response)
        entered.set()
        await gate.wait()

    handler._process_stream_impl = process
    env = SimpleNamespace(request_id=continuation.id, channel="web", method="chat.send",
                          user_id="", params={})
    await handler._start_stream_task(continuation, env, continuation.id)
    task = handler._stream_tasks[continuation.id]
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        task.cancel()
    else:
        gate.set()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    response = await handler.consume_robot_messages(timeout=None)
    assert telemetry.get_message_trace(response) is op
    telemetry.release_response_trace(response)
    assert op.pending_deliveries == 0
    assert not op.producer_done and not op.finished
    op.complete()
    assert op.finished


@pytest.mark.asyncio
async def test_supplement_unary_response_retains_original_producer(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    telemetry.retain_request_continuation(continuation, op)
    env = SimpleNamespace(request_id=continuation.id, channel="web", method="chat.send")
    response = SimpleNamespace(request_id=continuation.id, channel_id="web", ok=True)
    handler._send_non_stream_agent_request = AsyncMock(return_value=response)
    handler._response_to_message = lambda *args, **kwargs: message(continuation.id, kind="res")
    handler._is_interrupt_evolution_approval_chat_send = lambda *args, **kwargs: False
    handler._notify_chat_completed = lambda *args, **kwargs: None
    assert await handler._process_non_stream_request(continuation, env, complete_trace=True) is response
    queued = await handler.consume_robot_messages(timeout=None)
    assert queued.id == continuation.id and queued.id != op.request_id
    assert telemetry.get_message_trace(queued) is op
    assert hasattr(queued, "_gateway_trace_delivery")
    telemetry.release_response_trace(queued)
    telemetry.release_request_continuation(continuation)
    assert op.pending_deliveries == 0
    assert not op.producer_done and not op.finished
    op.complete()
    assert op.finished


@pytest.mark.asyncio
async def test_supplement_forward_early_continue_does_not_complete_original_producer(monkeypatch):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    handler._is_session_input_message = lambda msg: False
    handler._handle_channel_control = AsyncMock(return_value=True)
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    telemetry.retain_request_continuation(continuation, op)
    reference = continuation._gateway_trace_continuation
    await handler.start_forwarding()
    try:
        await handler.publish_user_messages(continuation)
        async with asyncio.timeout(2):
            while not reference.released:
                await asyncio.sleep(0)
        assert handler._handle_channel_control.await_count == 1
        assert op.pending_deliveries == 0
        assert not op.producer_done and not op.finished
        op.complete()
        assert op.finished
    finally:
        await handler.stop_forwarding()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["unknown_team", "non_stream_team", "hook_update"])
async def test_supplement_early_response_retains_explicit_parent(monkeypatch, path):
    op = operation(monkeypatch)
    monkeypatch.setattr(MessageHandler, "_instance", None)
    handler = MessageHandler(SimpleNamespace())
    source = message(kind="req")
    telemetry.attach_message_trace(source, op)
    continuation = handler._build_queued_chat_send_message(source, "follow up")
    telemetry.retain_request_continuation(continuation, op)
    assert continuation.id != op.request_id
    with telemetry.bind_request_trace(op):
        if path == "unknown_team":
            await handler._reject_unknown_team_mode_send(continuation)
        elif path == "non_stream_team":
            await handler._reject_non_stream_team_send(continuation)
        else:
            await handler._publish_hook_params_update_if_changed(continuation, {"query": "previous"})
    queued = await handler.consume_robot_messages(timeout=None)
    assert queued.id == continuation.id
    assert telemetry.get_message_trace(queued) is op
    assert hasattr(queued, "_gateway_trace_delivery")
    op.complete()
    telemetry.release_request_continuation(continuation)
    assert not op.finished
    telemetry.release_response_trace(queued)
    assert op.finished
