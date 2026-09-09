"""Focused tests for the WebSocket stream keepalive task owner."""

# White-box lifecycle tests intentionally exercise private owner state, and
# their one-purpose runtime/socket fakes intentionally expose a single method.
# pylint: disable=protected-access,too-few-public-methods

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed
from websockets.legacy.client import connect as websocket_connect
from websockets.legacy.server import serve as websocket_serve

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server


@pytest.fixture(autouse=True)
def _disable_persistent_checkpointer(monkeypatch) -> None:
    """Keep 0.2.6 stream tests independent from persistent runtime setup."""
    async def no_checkpointer_setup() -> None:
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep."
        "ensure_persistent_checkpointer",
        no_checkpointer_setup,
    )


def _server_for_stream(runtime):
    """Adapt a focused stream fake to the 0.2.6 AgentServer path."""
    class RuntimeAgent:
        """Expose the legacy Agent stream interface used by 0.2.6."""

        def process_message_stream(self, request):
            """Delegate to the focused async stream fake."""
            return runtime.stream(
                request,
                trigger_hook=False,
                on_control_event=None,
            )

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._session_stream_tasks = {}
    server._is_readonly_goal_get_request = lambda request: True
    server._is_stateless_method_request = lambda request: True

    async def no_heartbeat_start() -> None:
        return None

    async def get_agent(channel_id):
        del channel_id
        return RuntimeAgent()

    server._try_start_heartbeat_runtime = no_heartbeat_start
    server._get_stateless_agent = get_agent
    return server


@pytest.mark.asyncio
async def test_keepalive_child_is_cancelled_when_stop_owner_is_cancelled() -> None:
    """Owner cancellation must not orphan its keepalive child task."""
    keepalive_cancelled = asyncio.Event()

    async def keepalive() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            keepalive_cancelled.set()
            raise

    keepalive_task = asyncio.create_task(keepalive())
    stop_task = asyncio.create_task(
        agent_ws_server._stop_stream_keepalive(  # pylint: disable=protected-access,no-member
            keepalive_task,
            asyncio.Event(),
            asyncio.Event(),
            "stream-keepalive-owner-cancelled",
        )
    )
    await asyncio.sleep(0)
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task
    await asyncio.wait_for(keepalive_cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0)

    assert keepalive_task.cancelled()


@pytest.mark.asyncio
async def test_keepalive_failure_is_logged_without_escaping(
    monkeypatch,
) -> None:
    """An auxiliary send failure must not replace the stream outcome."""
    logged: list[tuple] = []
    monkeypatch.setattr(
        agent_ws_server.logger,
        "exception",
        lambda *args: logged.append(args),
    )

    async def fail_keepalive() -> None:
        raise RuntimeError("keepalive send failed")

    keepalive_task = asyncio.create_task(fail_keepalive())
    await asyncio.sleep(0)

    await agent_ws_server._stop_stream_keepalive(  # pylint: disable=protected-access,no-member
        keepalive_task,
        asyncio.Event(),
        asyncio.Event(),
        "stream-keepalive-failed",
    )

    assert len(logged) == 1
    assert logged[0][1] == "stream-keepalive-failed"


@pytest.mark.asyncio
async def test_cancelled_stream_owner_cleans_keepalive_and_registry() -> None:
    """Cancelling the stream owner must also cancel its keepalive child."""
    runtime_started = asyncio.Event()
    manager = object()

    class IdleRuntime:
        """Remain pending until cancellation reaches the stream owner."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Signal startup, then remain idle until cancelled."""
            del trigger_hook, on_control_event
            runtime_started.set()
            await asyncio.Event().wait()
            if request is None:
                yield AgentResponseChunk(
                    request_id="unreachable",
                    channel_id="web",
                    payload=None,
                )

    server = _server_for_stream(IdleRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-owner-cancelled",
        channel_id="web",
        session_id="session-keepalive-owner-cancelled",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )
    handler_task = asyncio.create_task(
        server._handle_stream_impl(object(), request, asyncio.Lock())
    )
    await asyncio.wait_for(runtime_started.wait(), timeout=0.1)
    handler_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await handler_task

    assert not server._session_stream_tasks
    assert not any(
        task.get_name() == "stream-keepalive:stream-keepalive-owner-cancelled"
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_closed_connection_cleans_keepalive_and_registry(monkeypatch) -> None:
    """A closed socket must end keepalive ownership without leaking state."""
    send_attempted = asyncio.Event()
    manager = object()

    class ClosedWebSocket:
        """Raise the transport's closed-connection exception on send."""

        async def send(self, payload: str) -> None:
            """Expose the attempt, then fail as a closed socket."""
            del payload
            send_attempted.set()
            raise WebSocketConnectionClosed(None, None)

    class IdleRuntime:
        """Finish after the keepalive observes the closed connection."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Wait for the failed keepalive send, then finish."""
            del trigger_hook, on_control_event
            await send_attempted.wait()
            if request is None:
                yield AgentResponseChunk(
                    request_id="unreachable",
                    channel_id="web",
                    payload=None,
                )

    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.001,
    )
    server = _server_for_stream(IdleRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-connection-closed",
        channel_id="web",
        session_id="session-keepalive-connection-closed",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )

    await server._handle_stream_impl(
        ClosedWebSocket(),
        request,
        asyncio.Lock(),
    )

    assert send_attempted.is_set()
    assert not server._session_stream_tasks
    assert not any(
        task.get_name() == "stream-keepalive:stream-keepalive-connection-closed"
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_stream_close_error_still_cleans_keepalive_and_registry() -> None:
    """Runtime close errors must not bypass transport-owner cleanup."""
    manager = object()

    class CloseFailingStream:
        """Async stream that fails during explicit close."""

        def __aiter__(self):
            """Return this async iterator."""
            return self

        async def __anext__(self):
            """Finish without yielding an event."""
            raise StopAsyncIteration

        async def aclose(self) -> None:
            """Expose the cleanup failure under test."""
            raise RuntimeError("stream close failed")

    class CloseFailingRuntime:  # pylint: disable=too-few-public-methods
        """Return a stream whose cleanup fails."""

        agent_manager = manager

        def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Create the close-failing stream."""
            del request, trigger_hook, on_control_event
            return CloseFailingStream()

    server = _server_for_stream(CloseFailingRuntime())
    request = AgentRequest(
        request_id="stream-close-error-cleanup",
        channel_id="web",
        session_id="session-close-error-cleanup",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )

    with pytest.raises(RuntimeError, match="stream close failed"):
        await server._handle_stream_impl(  # pylint: disable=protected-access
            object(),
            request,
            asyncio.Lock(),
        )

    assert not server._session_stream_tasks  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_stream_keepalive_roundtrips_over_live_websocket(monkeypatch) -> None:
    """A live socket keeps ordering and leaves no keepalive owner behind."""
    release_runtime = asyncio.Event()
    stream_finished = asyncio.Event()
    hold_connection_open = asyncio.Event()

    class LiveRuntime:
        """Yield one terminal event after the client observes a keepalive."""

        agent_manager = object()

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Hold the stream idle, then yield its terminal event."""
            del trigger_hook, on_control_event
            await release_runtime.wait()
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "done"},
                is_complete=True,
            )

    server = _server_for_stream(LiveRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-live-socket",
        channel_id="web",
        session_id="session-keepalive-live-socket",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
        agent_ref={"mode": "agent", "id": "live-socket-agent"},
    )
    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.01,
    )

    async def handle_connection(websocket) -> None:
        try:
            await server._handle_stream_impl(  # pylint: disable=protected-access
                websocket,
                request,
                asyncio.Lock(),
            )
            stream_finished.set()
            await hold_connection_open.wait()
        finally:
            stream_finished.set()

    listener = await websocket_serve(
        handle_connection,
        "127.0.0.1",
        0,
        ping_interval=None,
    )
    try:
        port = listener.sockets[0].getsockname()[1]
        async with websocket_connect(
            f"ws://127.0.0.1:{port}",
            ping_interval=None,
        ) as client:
            first_wire = json.loads(await asyncio.wait_for(client.recv(), timeout=0.5))
            first_chunk = parse_agent_server_wire_chunk(first_wire)
            assert first_wire["sequence"] == -1
            assert first_chunk.payload["event_type"] == "keepalive"

            release_runtime.set()
            received_wires = [first_wire]
            while True:
                wire = json.loads(await asyncio.wait_for(client.recv(), timeout=0.5))
                received_wires.append(wire)
                if parse_agent_server_wire_chunk(wire).is_complete:
                    break

            await asyncio.wait_for(stream_finished.wait(), timeout=0.5)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(client.recv(), timeout=0.03)

            assert received_wires[-1]["sequence"] == 0
            assert all(wire["sequence"] == -1 for wire in received_wires[:-1])
            assert not server._session_stream_tasks  # pylint: disable=protected-access
            assert not any(
                task.get_name() == "stream-keepalive:stream-keepalive-live-socket"
                for task in asyncio.all_tasks()
            )
            hold_connection_open.set()
    finally:
        release_runtime.set()
        hold_connection_open.set()
        listener.close()
        await listener.wait_closed()


class _FakeWebSocket:  # pylint: disable=too-few-public-methods
    """Record wire payloads without opening a socket."""

    def __init__(self) -> None:
        """Create an empty payload record."""
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        """Record one serialized wire payload."""
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_stream_keepalive_shutdown_does_not_depend_on_task_cancellation(
    monkeypatch,
) -> None:
    """Cooperative stop must not depend on injecting task cancellation."""
    real_asyncio = asyncio
    wait_started = real_asyncio.Event()
    swallowed_cancellation = real_asyncio.Event()
    manager = object()

    async def swallow_first_cancellation(awaitable, *, timeout):
        del timeout
        wait_started.set()
        try:
            return await awaitable
        except real_asyncio.CancelledError:
            if swallowed_cancellation.is_set():
                raise
            swallowed_cancellation.set()
            return True

    class AsyncioProxy:
        """Replace wait_for while delegating every other asyncio API."""

        wait_for = staticmethod(swallow_first_cancellation)

        def __getattr__(self, name):
            """Delegate an unmodified asyncio API."""
            return getattr(real_asyncio, name)

    class IdleRuntime:
        """End only after the keepalive owner enters its idle wait."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Wait for the keepalive loop, then finish without a chunk."""
            del trigger_hook, on_control_event
            await wait_started.wait()
            if request is None:
                yield AgentResponseChunk(
                    request_id="unreachable",
                    channel_id="web",
                    payload=None,
                )

    monkeypatch.setattr(agent_ws_server, "asyncio", AsyncioProxy())
    server = _server_for_stream(IdleRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-stop",
        channel_id="web",
        session_id="session-keepalive-stop",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )
    ws = _FakeWebSocket()

    handler_task = real_asyncio.create_task(
        server._handle_stream_impl(ws, request, real_asyncio.Lock())
    )
    done, _ = await real_asyncio.wait({handler_task}, timeout=0.1)
    completed_without_cancellation = handler_task in done
    if not completed_without_cancellation:
        handler_task.cancel()
        await real_asyncio.gather(handler_task, return_exceptions=True)

    assert completed_without_cancellation is True
    assert swallowed_cancellation.is_set() is False
    assert not ws.sent
    assert not server._session_stream_tasks


@pytest.mark.asyncio
async def test_stream_keepalive_cleanup_is_bounded_when_cancellation_is_ignored(
    monkeypatch,
) -> None:
    """Owner cleanup must remain bounded if a socket send ignores cancel."""
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    send_finished = asyncio.Event()
    manager = object()

    class StubbornWebSocket:
        """Keep send pending even after its first cancellation."""

        async def send(self, payload: str) -> None:
            """Wait for explicit release and swallow the first cancellation."""
            del payload
            send_started.set()
            try:
                await release_send.wait()
            except asyncio.CancelledError:
                await release_send.wait()
            finally:
                send_finished.set()

    class IdleRuntime:
        """Finish after the keepalive send has become stuck."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Wait for the stuck keepalive send, then finish."""
            del trigger_hook, on_control_event
            await send_started.wait()
            if request is None:
                yield AgentResponseChunk(
                    request_id="unreachable",
                    channel_id="web",
                    payload=None,
                )

    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_STOP_TIMEOUT_SECONDS",
        0.01,
    )
    server = _server_for_stream(IdleRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-bounded-stop",
        channel_id="web",
        session_id="session-keepalive-bounded-stop",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )

    await asyncio.wait_for(
        server._handle_stream_impl(
            StubbornWebSocket(),
            request,
            asyncio.Lock(),
        ),
        timeout=0.2,
    )

    assert not server._session_stream_tasks
    assert send_finished.is_set() is False
    release_send.set()
    await asyncio.wait_for(send_finished.wait(), timeout=0.1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stream_keepalive_is_sent_only_before_terminal_chunk(
    monkeypatch,
) -> None:
    """Terminal delivery must permanently stop subsequent keepalive sends."""
    keepalive_sent = asyncio.Event()
    release_stream = asyncio.Event()
    manager = object()

    class SignallingWebSocket(_FakeWebSocket):
        """Record sends and expose the first keepalive as an event."""

        async def send(self, payload: str) -> None:
            """Record payload and signal when it is a keepalive."""
            await super().send(payload)
            if json.loads(payload).get("sequence") == -1:
                keepalive_sent.set()

    class SlowRuntime:
        """Remain idle long enough for a keepalive, then terminate."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Yield one terminal event after explicit release."""
            del trigger_hook, on_control_event
            await release_stream.wait()
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "done"},
                is_complete=True,
            )

    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.01,
    )
    server = _server_for_stream(SlowRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-terminal",
        channel_id="web",
        session_id="session-keepalive-terminal",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
        agent_ref={"mode": "agent", "id": "keepalive-agent"},
    )
    ws = SignallingWebSocket()

    handler_task = asyncio.create_task(
        server._handle_stream_impl(ws, request, asyncio.Lock())
    )
    await asyncio.wait_for(keepalive_sent.wait(), timeout=0.2)
    release_stream.set()
    await asyncio.wait_for(handler_task, timeout=0.2)

    sent_after_completion = len(ws.sent)
    await asyncio.sleep(0.03)
    wires = [json.loads(payload) for payload in ws.sent]
    keepalive_wires = wires[:-1]
    assert len(ws.sent) == sent_after_completion
    assert wires[-1]["sequence"] == 0
    assert all(wire["sequence"] == -1 for wire in keepalive_wires)
    assert all(
        wire["agent_ref"] == {"mode": "agent", "id": "keepalive-agent"}
        for wire in keepalive_wires
    )
    assert all(
        parse_agent_server_wire_chunk(wire).payload["event_type"] == "keepalive"
        for wire in keepalive_wires
    )
    assert not server._session_stream_tasks


@pytest.mark.asyncio
async def test_stream_keepalive_rechecks_activity_after_waiting_for_send_lock(
    monkeypatch,
) -> None:
    """A keepalive queued on the send lock must honor newer stream activity."""
    first_waiter = asyncio.Event()
    both_waiters = asyncio.Event()
    manager = object()

    class ContendedLock:
        """Hold both transport writers until their ordering is observable."""

        def __init__(self) -> None:
            """Create an initially available lock and waiter counter."""
            self._lock = asyncio.Lock()
            self._waiter_count = 0

        async def hold(self) -> None:
            """Acquire the underlying lock before starting either writer."""
            await self._lock.acquire()

        def release(self) -> None:
            """Release the initial test hold."""
            self._lock.release()

        async def __aenter__(self):
            """Record writer order before waiting for the underlying lock."""
            self._waiter_count += 1
            if self._waiter_count == 1:
                first_waiter.set()
            if self._waiter_count == 2:
                both_waiters.set()
            await self._lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            """Release the underlying lock after one transport send attempt."""
            del exc_type, exc, traceback
            self._lock.release()

    class ActiveRuntime:
        """Produce real activity after the keepalive is already lock-queued."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Yield one active chunk and one terminal chunk."""
            del trigger_hook, on_control_event
            await first_waiter.wait()
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.delta", "content": "active"},
            )
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "done"},
                is_complete=True,
            )

    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.001,
    )
    server = _server_for_stream(ActiveRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-send-lock-activity",
        channel_id="web",
        session_id="session-keepalive-send-lock-activity",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )
    send_lock = ContendedLock()
    await send_lock.hold()
    ws = _FakeWebSocket()
    handler_task = asyncio.create_task(
        server._handle_stream_impl(ws, request, send_lock)
    )

    await asyncio.wait_for(both_waiters.wait(), timeout=0.2)
    send_lock.release()
    await asyncio.wait_for(handler_task, timeout=0.2)

    assert [json.loads(payload)["sequence"] for payload in ws.sent] == [0, 1]
    assert not server._session_stream_tasks


@pytest.mark.asyncio
async def test_stream_keepalive_rechecks_stop_after_waiting_for_send_lock(
    monkeypatch,
) -> None:
    """A keepalive queued on the send lock must recheck terminal stop."""
    lock_wait_started = asyncio.Event()
    release_lock = asyncio.Event()
    manager = object()

    class DelayedLock:
        """Hold lock entry until stream cleanup has signalled stop."""

        async def __aenter__(self):
            """Signal lock contention and wait for explicit release."""
            lock_wait_started.set()
            await release_lock.wait()

        async def __aexit__(self, exc_type, exc, traceback):
            """Release the synthetic lock context."""
            del exc_type, exc, traceback

    class IdleRuntime:
        """Finish while the keepalive is queued for the send lock."""

        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            """Release lock entry only after owner cleanup can signal stop."""
            del trigger_hook, on_control_event
            await lock_wait_started.wait()

            async def release_after_stream_cleanup_starts() -> None:
                await asyncio.sleep(0)
                release_lock.set()

            asyncio.create_task(release_after_stream_cleanup_starts())
            if request is None:
                yield AgentResponseChunk(
                    request_id="unreachable",
                    channel_id="web",
                    payload=None,
                )

    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.001,
    )
    server = _server_for_stream(IdleRuntime())
    request = AgentRequest(
        request_id="stream-keepalive-send-lock-stop",
        channel_id="web",
        session_id="session-keepalive-send-lock-stop",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )
    ws = _FakeWebSocket()

    await server._handle_stream_impl(ws, request, DelayedLock())

    assert not ws.sent
    assert not server._session_stream_tasks
