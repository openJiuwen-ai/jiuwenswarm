import asyncio
import time

import pytest

from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import (
    A2A_THOUGHT_METADATA_KEY,
    A2AChannel,
    A2AChannelConfig,
    _A2AAgentExecutor,
)
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod


class DummyBus:
    @staticmethod
    async def publish_user_messages(msg):
        return None


class DummyPart:
    def __init__(
        self,
        *,
        text: str = "",
        filename: str = "",
        media_type: str = "",
        url: str = "",
        data: str = "",
        raw: str = "",
    ):
        self.text = text
        self.filename = filename
        self.media_type = media_type
        self.url = url
        self.data = data
        self.raw = raw


class DummyA2AMessage:
    def __init__(self, parts):
        self.parts = parts


def build_channel() -> A2AChannel:
    return A2AChannel(A2AChannelConfig(enabled=False), DummyBus())


def test_map_a2a_parts_to_params_forwards_text_but_drops_files():
    msg = DummyA2AMessage(
        [
            DummyPart(text="hello"),
            DummyPart(
                filename="sample-url.txt",
                media_type="text/plain",
                url="https://example.com/test.txt",
            ),
            DummyPart(
                filename="inline.txt",
                media_type="text/plain",
                data="aGVsbG8gd29ybGQ=",
            ),
            DummyPart(raw="opaque-bytes"),
        ]
    )

    query, files = A2AChannel.map_a2a_parts_to_params(msg)

    assert query == "hello"
    assert files == []


def test_filter_ingress_metadata_keeps_only_bounded_tracing_fields():
    filtered = A2AChannel.filter_ingress_metadata(
        {
            "trace_id": "trace-1",
            "traceparent": "x" * 600,
            "cwd": "C:/sensitive",
            "member_name": "admin",
            "ws_id": "forged",
            "user_id": "forged-user",
            "enable_memory": False,
            "correlation_id": 123,
        }
    )

    assert filtered == {"trace_id": "trace-1", "traceparent": "x" * 512}


def test_message_to_a2a_parts_filters_completion_sentinel_text():
    pytest.importorskip("a2a.types")

    msg = Message(
        id="r1",
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "{'is_complete': True}"},
        event_type=EventType.CHAT_FINAL,
    )

    parts = A2AChannel.message_to_a2a_parts(msg, fallback_to_text=False)
    assert parts == []


def test_message_to_a2a_parts_maps_tool_events():
    pytest.importorskip("a2a.types")

    tool_call_msg = Message(
        id="r2",
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"tool_call": {"name": "view_file"}},
        event_type=EventType.CHAT_TOOL_CALL,
    )
    tool_result_msg = Message(
        id="r3",
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"tool_name": "view_file", "result": "ok"},
        event_type=EventType.CHAT_TOOL_RESULT,
    )

    call_parts = A2AChannel.message_to_a2a_parts(tool_call_msg, fallback_to_text=False)
    result_parts = A2AChannel.message_to_a2a_parts(
        tool_result_msg, fallback_to_text=False
    )

    assert len(call_parts) == 1
    assert getattr(call_parts[0], "text", "") == "[tool_call] view_file"
    assert len(result_parts) == 2
    assert getattr(result_parts[0], "text", "") == "ok"
    assert getattr(result_parts[1], "text", "") == "[tool_result:view_file] ok"


def test_is_terminal_message_rules():
    terminal_error = Message(
        id="e1",
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=False,
        payload={"error": "boom"},
        event_type=EventType.CHAT_ERROR,
    )
    terminal_complete = Message(
        id="e2",
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"is_complete": True},
        event_type=EventType.CHAT_FINAL,
    )
    non_terminal = Message(
        id="e3",
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "delta"},
        event_type=EventType.CHAT_DELTA,
    )

    assert A2AChannel.is_terminal_message(terminal_error) is True
    assert A2AChannel.is_terminal_message(terminal_complete) is True
    assert A2AChannel.is_terminal_message(non_terminal) is False


@pytest.mark.asyncio
async def test_dispatch_a2a_request_requires_on_message_callback():
    channel = build_channel()

    with pytest.raises(RuntimeError, match="on_message callback"):
        await channel.dispatch_a2a_request(
            request_id="req-1",
            session_id="sess-1",
            query="hello",
        )


@pytest.mark.asyncio
async def test_dispatch_a2a_request_and_send_queue_roundtrip():
    channel = build_channel()
    seen = []

    async def on_message(msg: Message):
        seen.append(msg)

    channel.on_message(on_message)
    pending = await channel.dispatch_a2a_request(
        request_id="req-2",
        session_id="sess-2",
        query="hello",
        files=[{"filename": "x.txt", "data": "aGVsbG8="}],
        metadata={"trace_id": "t-1"},
    )

    assert len(seen) == 1
    outbound = seen[0]
    assert outbound.id == "req-2"
    assert outbound.session_id == "sess-2"
    assert outbound.params["query"] == "hello"
    assert outbound.params["files"][0]["filename"] == "x.txt"
    assert outbound.metadata == {"trace_id": "t-1"}

    inbound = Message(
        id="req-2",
        type="event",
        channel_id="a2a",
        session_id="sess-2",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "ok", "is_complete": True},
        event_type=EventType.CHAT_FINAL,
    )
    await channel.send(inbound)
    queued = await pending.queue.get()
    assert queued.payload["content"] == "ok"


@pytest.mark.asyncio
async def test_dispatch_a2a_request_defaults_metadata_to_empty_dict():
    """A2A ingress must not leave Message.metadata as None (breaks MessageHandler)."""
    channel = build_channel()
    seen = []

    async def on_message(msg: Message):
        seen.append(msg)

    channel.on_message(on_message)
    await channel.dispatch_a2a_request(
        request_id="req-empty-md",
        session_id="sess-empty-md",
        query="hello",
    )

    assert len(seen) == 1
    assert seen[0].metadata == {}


@pytest.mark.asyncio
async def test_cancel_pending_request_wakes_executor_queue():
    channel = build_channel()
    seen = []
    channel.on_message(seen.append)
    pending = await channel.dispatch_a2a_request(
        request_id="req-cancel",
        session_id="sess-cancel",
        query="hello",
    )

    assert await channel.cancel_pending_request("req-cancel") is True
    canceled = await pending.queue.get()
    assert canceled.event_type == EventType.CHAT_INTERRUPT_RESULT
    assert canceled.payload["is_complete"] is True
    assert [msg.req_method for msg in seen] == [
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_CANCEL,
    ]
    assert seen[1].session_id == "sess-cancel"
    assert seen[1].params == {"intent": "cancel", "session_id": "sess-cancel"}


@pytest.mark.asyncio
async def test_stop_cancels_every_pending_agent_session():
    channel = build_channel()
    seen = []
    channel.on_message(seen.append)
    await channel.dispatch_a2a_request(
        request_id="req-stop",
        session_id="sess-stop",
        query="hello",
    )

    await channel.stop()

    assert [msg.req_method for msg in seen] == [
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_CANCEL,
    ]
    assert seen[-1].session_id == "sess-stop"
    assert channel._pending == {}


@pytest.mark.asyncio
async def test_start_waits_for_real_uvicorn_readiness_and_mounts_extended_card(
    monkeypatch,
):
    release_start = asyncio.Event()
    instances = []
    advertised_cards = []

    from a2a.server import routes as a2a_routes

    create_agent_card_routes = a2a_routes.create_agent_card_routes

    def capture_agent_card_routes(agent_card, *, card_url):
        advertised_cards.append(agent_card)
        return create_agent_card_routes(agent_card, card_url=card_url)

    class DelayedServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            instances.append(self)

        async def serve(self, *, sockets):
            assert sockets and sockets[0].getsockname()[1] > 0
            await release_start.wait()
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr("uvicorn.Server", DelayedServer)
    monkeypatch.setattr(
        "a2a.server.routes.create_agent_card_routes", capture_agent_card_routes
    )
    channel = A2AChannel(
        A2AChannelConfig(enabled=True, host="127.0.0.1", port=0),
        DummyBus(),
    )
    start_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.25)

    assert start_task.done() is False
    release_start.set()
    await start_task
    paths = {route.path for route in instances[0].config.app.routes}
    assert channel.config.card_path in paths
    assert channel.config.extended_card_path in paths
    assert channel.config.rpc_path in paths
    assert len(advertised_cards) == 2
    assert all(not card.capabilities.extended_agent_card for card in advertised_cards)

    await channel.stop()


@pytest.mark.asyncio
async def test_start_surfaces_bind_failure_before_running(monkeypatch):
    def fail_bind(_host, _port):
        raise OSError("address already in use")

    monkeypatch.setattr(A2AChannel, "_bind_listen_socket", staticmethod(fail_bind))
    channel = A2AChannel(A2AChannelConfig(enabled=True), DummyBus())

    with pytest.raises(OSError, match="address already in use"):
        await channel.start()

    assert channel.is_running is False
    assert channel._server_task is None


def test_listen_socket_rejects_an_existing_listener():
    first = A2AChannel._bind_listen_socket("127.0.0.1", 0)
    try:
        port = first.getsockname()[1]
        with pytest.raises(OSError):
            A2AChannel._bind_listen_socket("127.0.0.1", port)
    finally:
        first.close()


@pytest.mark.asyncio
async def test_unexpected_uvicorn_exit_notifies_runtime_observer(monkeypatch):
    crash = asyncio.Event()
    observer_started = asyncio.Event()
    release_observer = asyncio.Event()

    class CrashingServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        async def serve(self, *, sockets):
            self.started = True
            await crash.wait()
            raise RuntimeError("server crashed")

    monkeypatch.setattr("uvicorn.Server", CrashingServer)
    channel = A2AChannel(
        A2AChannelConfig(enabled=True, host="127.0.0.1", port=0),
        DummyBus(),
    )
    observed = []

    async def observe(error):
        observed.append(error)
        observer_started.set()
        await release_observer.wait()

    channel.set_runtime_error_observer(observe)
    await channel.start()

    crash.set()
    await observer_started.wait()

    assert len(observed) == 1
    assert str(observed[0]) == "server crashed"
    assert channel.is_running is False
    assert len(channel._runtime_error_tasks) == 1
    observer_task = next(iter(channel._runtime_error_tasks))
    assert observer_task.done() is False
    release_observer.set()
    await observer_task
    await asyncio.sleep(0)
    assert channel._runtime_error_tasks == set()
    await channel.stop()


@pytest.mark.asyncio
async def test_executor_empty_query_emits_failed_task_lifecycle():
    pytest.importorskip("a2a.types")
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatusUpdateEvent
    from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import (
        _A2AAgentExecutor,
    )

    class MockEventQueue:
        def __init__(self) -> None:
            self.events: list = []
            self.closed = False

        async def enqueue_event(self, event) -> None:
            self.events.append(event)

        async def close(self, immediate: bool = False) -> None:
            self.closed = True

    class MockContext:
        def __init__(self) -> None:
            self.current_task = None
            self.task_id = "task-empty"
            self.context_id = "ctx-empty"
            self.metadata = {}
            self.message = Message(
                role=Role.ROLE_USER,
                parts=[Part(text="")],
                message_id="m-empty",
                task_id=self.task_id,
                context_id=self.context_id,
            )

        def get_user_input(self) -> str:
            return ""

    channel = build_channel()
    history_events = []
    channel.set_request_observer(history_events.append)

    event_queue = MockEventQueue()
    await _A2AAgentExecutor(channel).execute(MockContext(), event_queue)
    assert event_queue.closed is True
    assert len(event_queue.events) == 2
    assert isinstance(event_queue.events[0], Task)
    assert event_queue.events[0].id == "task-empty"
    status_event = event_queue.events[1]
    assert isinstance(status_event, TaskStatusUpdateEvent)
    assert status_event.task_id == "task-empty"
    assert status_event.context_id == "ctx-empty"
    assert status_event.status.state == TaskState.TASK_STATE_FAILED
    assert [event["status"] for event in history_events] == ["processing", "failed"]
    assert history_events[-1]["request_id"] == "task-empty"
    assert history_events[-1]["error"] == "empty query"


def _make_message(
    *,
    payload: dict,
    event_type: EventType,
    msg_id: str = "req-r",
) -> Message:
    return Message(
        id=msg_id,
        type="event",
        channel_id="a2a",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload=payload,
        event_type=event_type,
    )


def test_is_reasoning_message_detection():
    reasoning_event = _make_message(
        payload={"content": "let me think"},
        event_type=EventType.CHAT_REASONING,
    )
    reasoning_delta = _make_message(
        payload={"content": "thinking", "source_chunk_type": "llm_reasoning"},
        event_type=EventType.CHAT_DELTA,
    )
    plain_delta = _make_message(
        payload={"content": "answer"},
        event_type=EventType.CHAT_DELTA,
    )

    assert A2AChannel.is_reasoning_message(reasoning_event) is True
    assert A2AChannel.is_reasoning_message(reasoning_delta) is True
    assert A2AChannel.is_reasoning_message(plain_delta) is False


def test_message_to_a2a_parts_marks_thought_metadata():
    pytest.importorskip("a2a.types")

    reasoning_msg = _make_message(
        payload={"content": "let me think"},
        event_type=EventType.CHAT_REASONING,
    )
    plain_msg = _make_message(
        payload={"content": "final answer"},
        event_type=EventType.CHAT_DELTA,
    )

    thought_parts = A2AChannel.message_to_a2a_parts(
        reasoning_msg, fallback_to_text=False
    )
    plain_parts = A2AChannel.message_to_a2a_parts(plain_msg, fallback_to_text=False)

    assert len(thought_parts) == 1
    assert dict(thought_parts[0].metadata)[A2A_THOUGHT_METADATA_KEY] is True
    assert len(plain_parts) == 1
    assert A2A_THOUGHT_METADATA_KEY not in dict(plain_parts[0].metadata)


class _FakeEventQueue:
    def __init__(self):
        self.events = []
        self.closed = False

    async def enqueue_event(self, event):
        self.events.append(event)

    async def close(self, immediate: bool = False):
        self.closed = True


class _FakeContext:
    def __init__(self, query: str = "hello"):
        from a2a.types import Message as A2AMessage, Part, Role

        self.task_id = "task-1"
        self.context_id = "ctx-1"
        self.current_task = None
        self.message = A2AMessage(
            role=Role.ROLE_USER,
            parts=[Part(text=query)],
            message_id="m-fake",
            task_id=self.task_id,
            context_id=self.context_id,
        )
        self.metadata = {}

    def get_user_input(self):
        return "hello"


@pytest.mark.asyncio
async def test_executor_filters_caller_metadata_before_gateway_dispatch():
    pytest.importorskip("a2a.types")
    context = _FakeContext()
    context.metadata = {
        "trace_id": "trace-1",
        "cwd": "C:/forged",
        "member_name": "forged-member",
        "enable_memory": False,
    }
    channel = build_channel()
    captured = []

    async def on_message(msg: Message):
        captured.append(msg)
        pending = channel._pending[str(msg.id)]
        await pending.queue.put(
            Message(
                id=msg.id,
                type="event",
                channel_id="a2a",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "done", "is_complete": True},
                event_type=EventType.CHAT_FINAL,
            )
        )

    channel.on_message(on_message)
    await _A2AAgentExecutor(channel).execute(context, _FakeEventQueue())

    assert captured[0].metadata == {"trace_id": "trace-1"}


async def _run_executor_with_stream(channel: A2AChannel, stream: list[Message]):
    """Drive _A2AAgentExecutor.execute with a scripted message stream."""
    pytest.importorskip("a2a.types")
    executor = _A2AAgentExecutor(channel)
    event_queue = _FakeEventQueue()

    async def on_message(msg: Message):
        pending = channel._pending[str(msg.id)]
        for item in stream:
            replayed = Message(**{**item.__dict__, "id": msg.id})
            await pending.queue.put(replayed)

    channel.on_message(on_message)
    await executor.execute(_FakeContext(), event_queue)
    return event_queue


def _stream_reasoning_then_final() -> list[Message]:
    return [
        _make_message(
            payload={"content": "let me think", "source_chunk_type": "llm_reasoning"},
            event_type=EventType.CHAT_DELTA,
        ),
        _make_message(
            payload={"content": "final answer", "is_complete": True},
            event_type=EventType.CHAT_FINAL,
        ),
    ]


def _thought_status_updates(events) -> list:
    """Status updates whose message parts carry the thought metadata marker."""
    from a2a.types import TaskStatusUpdateEvent

    result = []
    for event in events:
        if not isinstance(event, TaskStatusUpdateEvent):
            continue
        if not event.status.HasField("message"):
            continue
        for part in event.status.message.parts:
            if dict(part.metadata).get(A2A_THOUGHT_METADATA_KEY):
                result.append(event)
                break
    return result


@pytest.mark.asyncio
async def test_executor_streams_reasoning_as_thought_status_updates_by_default():
    pytest.importorskip("a2a.types")
    from a2a.types import TaskArtifactUpdateEvent

    channel = build_channel()
    history_events = []
    channel.set_request_observer(history_events.append)
    event_queue = await _run_executor_with_stream(
        channel, _stream_reasoning_then_final()
    )

    artifact_events = [
        e for e in event_queue.events if isinstance(e, TaskArtifactUpdateEvent)
    ]
    assert len(artifact_events) == 1
    assert [p.text for p in artifact_events[0].artifact.parts] == ["final answer"]

    thought_updates = _thought_status_updates(event_queue.events)
    assert len(thought_updates) == 1
    thought_parts = list(thought_updates[0].status.message.parts)
    assert thought_parts[0].text == "let me think"
    assert dict(thought_parts[0].metadata)[A2A_THOUGHT_METADATA_KEY] is True
    assert [event["status"] for event in history_events] == ["processing", "completed"]


@pytest.mark.asyncio
async def test_executor_cancellation_records_terminal_history_and_reraises():
    pytest.importorskip("a2a.types")
    channel = build_channel()
    history_events = []
    callback_started = asyncio.Event()

    async def on_message(msg: Message):
        callback_started.set()
        await asyncio.Event().wait()

    channel.on_message(on_message)
    channel.set_request_observer(history_events.append)
    event_queue = _FakeEventQueue()
    execute_task = asyncio.create_task(
        _A2AAgentExecutor(channel).execute(_FakeContext(), event_queue)
    )
    await callback_started.wait()
    execute_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execute_task
    assert [event["status"] for event in history_events] == ["processing", "canceled"]
    assert history_events[-1]["error"] is None
    assert event_queue.closed is True


@pytest.mark.asyncio
async def test_executor_cancel_closes_event_queue_without_immediate():
    pytest.importorskip("a2a.types")

    class TrackingQueue:
        def __init__(self) -> None:
            self.events: list = []
            self.close_immediate: bool | None = None

        async def enqueue_event(self, event) -> None:
            self.events.append(event)

        async def close(self, immediate: bool = False) -> None:
            self.close_immediate = immediate

    channel = build_channel()
    queue = TrackingQueue()
    await _A2AAgentExecutor(channel).cancel(_FakeContext(), queue)

    assert queue.close_immediate is False
    assert queue.events


@pytest.mark.asyncio
async def test_executor_interrupt_survives_closed_event_queue():
    pytest.importorskip("a2a.types")
    channel = build_channel()
    dispatched = asyncio.Event()
    history_events = []

    async def on_message(msg: Message):
        dispatched.set()

    channel.on_message(on_message)
    channel.set_request_observer(history_events.append)

    class ClosedQueue:
        def __init__(self) -> None:
            self.closed = False
            self.close_immediate: bool | None = None

        async def enqueue_event(self, event) -> None:
            if self.closed:
                raise RuntimeError("queue is closed")

        async def close(self, immediate: bool = False) -> None:
            self.closed = True
            self.close_immediate = immediate

    context = _FakeContext()
    queue = ClosedQueue()
    execute_task = asyncio.create_task(
        _A2AAgentExecutor(channel).execute(context, queue)
    )
    await dispatched.wait()
    await _A2AAgentExecutor(channel).cancel(context, queue)
    await execute_task

    assert queue.close_immediate is False
    assert history_events[-1]["status"] == "canceled"


@pytest.mark.asyncio
async def test_executor_history_redacts_internal_exception_text():
    pytest.importorskip("a2a.types")
    channel = build_channel()
    history_events = []

    async def on_message(msg: Message):
        raise RuntimeError("secret path C:/internal/service")

    channel.on_message(on_message)
    channel.set_request_observer(history_events.append)
    await _A2AAgentExecutor(channel).execute(_FakeContext(), _FakeEventQueue())

    assert history_events[-1]["status"] == "failed"
    assert history_events[-1]["error"] == "request execution failed"
    assert "secret" not in history_events[-1]["error"]


@pytest.mark.asyncio
async def test_executor_history_uses_display_safe_agent_error_summary():
    pytest.importorskip("a2a.types")
    channel = build_channel()
    history_events = []
    channel.set_request_observer(history_events.append)
    await _run_executor_with_stream(
        channel,
        [
            _make_message(
                payload={"error": "internal host db.private.local failed"},
                event_type=EventType.CHAT_ERROR,
            )
        ],
    )

    assert history_events[-1]["status"] == "failed"
    assert history_events[-1]["error"] == "agent request failed"
    assert "db.private.local" not in history_events[-1]["error"]


@pytest.mark.asyncio
async def test_executor_drops_reasoning_when_disabled():
    pytest.importorskip("a2a.types")
    from a2a.types import TaskArtifactUpdateEvent

    channel = A2AChannel(
        A2AChannelConfig(enabled=False, expose_reasoning=False), DummyBus()
    )
    event_queue = await _run_executor_with_stream(
        channel, _stream_reasoning_then_final()
    )

    artifact_events = [
        e for e in event_queue.events if isinstance(e, TaskArtifactUpdateEvent)
    ]
    assert len(artifact_events) == 1
    artifact_texts = [p.text for p in artifact_events[0].artifact.parts]
    assert artifact_texts == ["final answer"]
    assert _thought_status_updates(event_queue.events) == []


@pytest.mark.asyncio
async def test_executor_terminal_reasoning_chunk_does_not_leak_into_artifact():
    pytest.importorskip("a2a.types")
    from a2a.types import TaskArtifactUpdateEvent, TaskState

    channel = build_channel()
    stream = [
        _make_message(
            payload={"content": "answer part"},
            event_type=EventType.CHAT_DELTA,
        ),
        _make_message(
            payload={
                "content": "trailing thought",
                "source_chunk_type": "llm_reasoning",
                "is_complete": True,
            },
            event_type=EventType.CHAT_DELTA,
        ),
    ]
    event_queue = await _run_executor_with_stream(channel, stream)

    artifact_events = [
        e for e in event_queue.events if isinstance(e, TaskArtifactUpdateEvent)
    ]
    all_texts = [p.text for e in artifact_events for p in e.artifact.parts]
    assert "trailing thought" not in all_texts
    assert "answer part" in all_texts
    final_states = [
        e.status.state
        for e in event_queue.events
        if not isinstance(e, TaskArtifactUpdateEvent)
    ]
    assert TaskState.TASK_STATE_COMPLETED in final_states
