import asyncio
import json
import logging
import weakref
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.common.e2a.gateway_normalize import (
    build_fallback_e2a,
    e2a_from_agent_fields,
)
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.invocation_context import get_current_invocation_context
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class ClosedFakeWebSocket:
    remote_address = ("127.0.0.1", 1)

    async def send(self, payload: str) -> None:
        raise ConnectionClosedError(None, None)


class _AgentWsTestHarness(AgentWebSocketServer):
    async def handle_message_for_test(self, ws, raw: str, send_lock: asyncio.Lock) -> None:
        await self._handle_message(ws, raw, send_lock)


class ClosedDuringUnaryServer(_AgentWsTestHarness):
    async def _handle_unary(self, ws, request, send_lock) -> None:
        raise ConnectionClosedError(None, None)


class TraceObservedUnaryServer(_AgentWsTestHarness):
    def __init__(self) -> None:
        super().__init__()
        self.observed_trace = None

    async def _handle_unary(self, ws, request, send_lock) -> None:
        invocation = get_current_invocation_context()
        self.observed_trace = invocation.trace if invocation is not None else None


class _FakeInterruptAgent:
    async def process_message(self, request):
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"event_type": "chat.interrupt_result", "success": True},
        )


class _CleanupRecordingAgentManager:
    def __init__(self) -> None:
        self.cleaned: list[tuple[str, str]] = []
        self.agent = _FakeInterruptAgent()

    def get_agent_nowait(self, *_args, **_kwargs):
        return self.agent

    async def get_agent(self, **_kwargs):
        return self.agent

    async def cleanup_session_runtime(self, *, channel_id: str, session_id: str) -> bool:
        self.cleaned.append((channel_id, session_id))
        return True


class _NoCreateCleanupAgentManager:
    def __init__(self) -> None:
        self.cleaned: list[tuple[str, str]] = []

    def get_agent_nowait(self, *_args, **_kwargs):
        return None

    async def get_agent(self, **_kwargs):
        raise AssertionError("client disconnect cancel must not create an agent")

    async def cleanup_session_runtime(self, *, channel_id: str, session_id: str) -> bool:
        self.cleaned.append((channel_id, session_id))
        return False


class _BlockingCleanupAgentManager(_CleanupRecordingAgentManager):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()

    async def cleanup_session_runtime(self, *, channel_id: str, session_id: str) -> bool:
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        return await super().cleanup_session_runtime(
            channel_id=channel_id,
            session_id=session_id,
        )


class _FailedCleanupAgentManager(_CleanupRecordingAgentManager):
    async def cleanup_session_runtime(self, *, channel_id: str, session_id: str) -> bool:
        raise RuntimeError("cleanup failed")


class _PinRecordingAgentManager:
    def __init__(self) -> None:
        self.pinned: list[object] = []
        self.unpinned: list[object] = []

    def pin_agent(self, agent: object) -> None:
        self.pinned.append(agent)

    def unpin_agent(self, agent: object) -> None:
        self.unpinned.append(agent)


async def _handle_cancel_cleanup_case(env) -> list[tuple[str, str]]:
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    manager = _CleanupRecordingAgentManager()
    server._agent_manager = manager
    server._session_stream_tasks = {}

    await server.handle_message_for_test(
        FakeWebSocket(),
        json.dumps(env.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )
    return manager.cleaned


@pytest.mark.asyncio
async def test_handle_message_treats_no_close_frame_as_disconnect(caplog) -> None:
    target_logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")
    target_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=target_logger.name)
    env = e2a_from_agent_fields(
        request_id="req-closed",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CONFIG_GET,
        params={},
        is_stream=False,
        timestamp=0.0,
    )
    try:
        await ClosedDuringUnaryServer().handle_message_for_test(
            FakeWebSocket(),
            json.dumps(env.to_dict(), ensure_ascii=False),
            asyncio.Lock(),
        )
    finally:
        target_logger.removeHandler(caplog.handler)

    assert "no close frame received or sent" in caplog.text
    assert "request_id=req-closed" in caplog.text


@pytest.mark.asyncio
async def test_handle_message_binds_trace_context_for_non_agent_dispatch() -> None:
    server = TraceObservedUnaryServer()
    env = e2a_from_agent_fields(
        request_id="req-trace",
        channel_id="xiaoyi",
        session_id="session-trace",
        req_method=ReqMethod.CONFIG_GET,
        params={},
        is_stream=False,
        timestamp=0.0,
        metadata={"xiaoyi_task_id": "root&19&abc&0"},
    )

    await server.handle_message_for_test(
        FakeWebSocket(),
        json.dumps(env.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert server.observed_trace is not None
    assert server.observed_trace.trace_id == "root&19&abc&0"
    assert get_current_invocation_context() is None


@pytest.mark.asyncio
async def test_handle_message_ignores_json_error_when_peer_is_closed(caplog) -> None:
    target_logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")
    target_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=target_logger.name)
    try:
        await _AgentWsTestHarness.__new__(_AgentWsTestHarness).handle_message_for_test(
            ClosedFakeWebSocket(),
            "not-json",
            asyncio.Lock(),
        )
    finally:
        target_logger.removeHandler(caplog.handler)

    assert "JSON" in caplog.text


@pytest.mark.asyncio
async def test_handle_message_reports_json_error_when_peer_is_open() -> None:
    ws = FakeWebSocket()
    await _AgentWsTestHarness.__new__(_AgentWsTestHarness).handle_message_for_test(
        ws,
        "not-json",
        asyncio.Lock(),
    )

    assert ws.sent[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_disconnect_cancel_cleans_session_runtime_after_cancel() -> None:
    session_id = "sess-exit"
    env = e2a_from_agent_fields(
        request_id="req-disconnect-cancel",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "cancel",
            "session_id": session_id,
        },
        is_stream=False,
        timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"
    agent_ws_server_module._plan_exited_sessions.add(session_id)

    try:
        assert await _handle_cancel_cleanup_case(env) == [("tui", session_id)]
        assert session_id not in agent_ws_server_module._plan_exited_sessions
    finally:
        agent_ws_server_module._plan_exited_sessions.discard(session_id)


@pytest.mark.asyncio
async def test_disconnect_cancel_response_waits_for_runtime_cleanup() -> None:
    session_id = "sess-cleanup-order"
    env = e2a_from_agent_fields(
        request_id="req-cleanup-order",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "session_id": session_id},
        is_stream=False,
        timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    manager = _BlockingCleanupAgentManager()
    server._agent_manager = manager
    server._session_stream_tasks = {}
    ws = FakeWebSocket()

    request_task = asyncio.create_task(
        server.handle_message_for_test(
            ws,
            json.dumps(env.to_dict(), ensure_ascii=False),
            asyncio.Lock(),
        )
    )
    await asyncio.wait_for(manager.cleanup_started.wait(), 1)

    assert ws.sent == []

    manager.allow_cleanup.set()
    await request_task

    assert manager.cleaned == [("tui", session_id)]
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_disconnect_cancel_response_reports_runtime_cleanup_failure() -> None:
    session_id = "sess-cleanup-failed"
    env = e2a_from_agent_fields(
        request_id="req-cleanup-failed",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "session_id": session_id},
        is_stream=False,
        timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    server._agent_manager = _FailedCleanupAgentManager()
    server._session_stream_tasks = {}
    ws = FakeWebSocket()

    await server.handle_message_for_test(
        ws,
        json.dumps(env.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert len(ws.sent) == 1
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is False
    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "success": False,
        "code": "CANCEL_FAILED",
        "error": "session runtime cleanup failed",
    }


def test_session_mode_sync_lock_cache_does_not_retain_idle_sessions() -> None:
    session_id = "sess-weak-lock"
    lock = AgentWebSocketServer._session_mode_sync_lock(session_id)
    lock_ref = weakref.ref(lock)

    assert agent_ws_server_module._session_mode_sync_locks.get(session_id) is lock

    del lock

    assert lock_ref() is None
    assert session_id not in agent_ws_server_module._session_mode_sync_locks


def test_scheduler_agent_pin_moves_with_persistent_owner() -> None:
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    manager = _PinRecordingAgentManager()
    first = object()
    second = object()
    server._agent_manager = manager
    server._scheduler_agent = None

    server._set_scheduler_agent(first)
    server._set_scheduler_agent(second)
    server._set_scheduler_agent(second)

    assert manager.pinned == [first, second]
    assert manager.unpinned == [first]
    assert server._scheduler_agent is second


@pytest.mark.asyncio
async def test_disconnect_cancel_does_not_create_agent_when_runtime_missing() -> None:
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    manager = _NoCreateCleanupAgentManager()
    server._agent_manager = manager
    server._session_stream_tasks = {}
    ws = FakeWebSocket()
    env = e2a_from_agent_fields(
        request_id="req-disconnect-no-agent",
        channel_id="tui",
        session_id="sess-no-agent",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "session_id": "sess-no-agent"},
        is_stream=False,
        timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"

    await server.handle_message_for_test(
        ws,
        json.dumps(env.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert manager.cleaned == [("tui", "sess-no-agent")]
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_disconnect_cancel_cleans_session_runtime_when_cancel_reply_send_fails() -> None:
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    manager = _CleanupRecordingAgentManager()
    server._agent_manager = manager
    server._session_stream_tasks = {}
    env = e2a_from_agent_fields(
        request_id="req-disconnect-cancel-send-fails",
        channel_id="tui",
        session_id="sess-send-fails",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "session_id": "sess-send-fails"},
        is_stream=False,
        timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"

    await server.handle_message_for_test(
        ClosedFakeWebSocket(),
        json.dumps(env.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert manager.cleaned == [("tui", "sess-send-fails")]


@pytest.mark.asyncio
async def test_disconnect_cancel_cleans_session_runtime_when_stream_task_cleanup_fails() -> None:
    started = asyncio.Event()

    async def failing_stream_task() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("stream cleanup failed")

    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    manager = _CleanupRecordingAgentManager()
    server._agent_manager = manager
    stream_task = asyncio.create_task(failing_stream_task())
    await asyncio.wait_for(started.wait(), 1)
    server._session_stream_tasks = {"sess-stream-cleanup-fails": {stream_task: asyncio.Event()}}
    env = e2a_from_agent_fields(
        request_id="req-disconnect-stream-cleanup-fails",
        channel_id="tui",
        session_id="sess-stream-cleanup-fails",
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "cancel",
            "session_id": "sess-stream-cleanup-fails",
        },
        is_stream=False,
        timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"

    await server.handle_message_for_test(
        FakeWebSocket(),
        json.dumps(env.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )
    assert manager.cleaned == [("tui", "sess-stream-cleanup-fails")]
    assert stream_task.done() is True


@pytest.mark.asyncio
async def test_cancel_source_param_does_not_trigger_session_runtime_cleanup() -> None:
    env = e2a_from_agent_fields(
        request_id="req-param-source",
        channel_id="tui",
        session_id="sess-param",
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "cancel",
            "session_id": "sess-param",
            "cancel_source": "client_disconnect",
        },
        is_stream=False,
        timestamp=0.0,
    )

    assert await _handle_cancel_cleanup_case(env) == []


@pytest.mark.asyncio
async def test_cancel_source_metadata_does_not_trigger_supplement_runtime_cleanup() -> None:
    env = e2a_from_agent_fields(
        request_id="req-metadata-source",
        channel_id="tui",
        session_id="sess-metadata",
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "supplement",
            "session_id": "sess-metadata",
        },
        is_stream=False,
        timestamp=0.0,
        metadata={"_jiuwenswarm_cancel_source": "client_disconnect"},
    )

    assert await _handle_cancel_cleanup_case(env) == []


@pytest.mark.asyncio
async def test_legacy_metadata_cancel_source_does_not_trigger_runtime_cleanup() -> None:
    env = build_fallback_e2a(
        {
            "request_id": "req-legacy-metadata-source",
            "channel_id": "tui",
            "session_id": "sess-legacy-metadata",
            "req_method": ReqMethod.CHAT_CANCEL.value,
            "params": {
                "intent": "cancel",
                "session_id": "sess-legacy-metadata",
            },
            "is_stream": False,
            "timestamp": 0.0,
            "metadata": {"_jiuwenswarm_cancel_source": "client_disconnect"},
        }
    )

    assert await _handle_cancel_cleanup_case(env) == []


@pytest.mark.asyncio
async def test_manual_cancel_keeps_session_runtime() -> None:
    env = e2a_from_agent_fields(
        request_id="req-manual-cancel",
        channel_id="tui",
        session_id="sess-keep",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "session_id": "sess-keep"},
        is_stream=False,
        timestamp=0.0,
    )

    assert await _handle_cancel_cleanup_case(env) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_request", [False, True])
async def test_disconnect_runtime_cleanup_remains_owned_after_rpc_leaves(cancel_request):
    sid = "disconnect-slow-runtime"
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    server._session_stream_tasks = {}
    server._stream_cancel_timeout = 0.02
    manager = _BlockingCleanupAgentManager()
    server._agent_manager = manager
    ws = FakeWebSocket()
    env = e2a_from_agent_fields(
        request_id="disconnect-slow", channel_id="tui", session_id=sid,
        req_method=ReqMethod.CHAT_CANCEL, params={"intent": "cancel", "session_id": sid},
        is_stream=False, timestamp=0.0,
    )
    env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"
    task = asyncio.create_task(server.handle_message_for_test(ws, json.dumps(env.to_dict()), asyncio.Lock()))
    try:
        await asyncio.wait_for(manager.cleanup_started.wait(), 1)
        if cancel_request:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            assert task in done, "RPC deadline must include runtime cleanup"
            assert ws.sent[-1]["status"] == "failed"
            assert ws.sent[-1]["body"]["details"]["code"] == "CANCEL_FAILED"
        server._handle_stream_impl = AsyncMock()
        chat = AgentRequest(request_id="new-disconnect-round", channel_id="desktop", session_id=sid,
                            req_method=ReqMethod.CHAT_SEND, params={"query": "hello"}, is_stream=True)
        await server._handle_stream(ws, chat, asyncio.Lock())
        server._handle_stream_impl.assert_not_awaited()
        assert manager.cleaned == []
        manager.allow_cleanup.set()
        await asyncio.gather(*list(server._stream_shutdown_jobs.values()))
        assert manager.cleaned == [("tui", sid)]
        await server._handle_stream(ws, chat, asyncio.Lock())
        server._handle_stream_impl.assert_awaited_once()
    finally:
        manager.allow_cleanup.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*list(getattr(server, "_stream_shutdown_jobs", {}).values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_joining_manual_stop_still_owns_runtime_cleanup():
    sid = "stop-then-disconnect"
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    server._channel_conns = {}
    server._session_stream_tasks = {}
    server._stream_cancel_timeout = 0.03
    manager = _BlockingCleanupAgentManager()
    server._agent_manager = manager
    cancel_entered, release_cancel = asyncio.Event(), asyncio.Event()
    cancel_calls = 0

    async def cancel_agent(request):
        nonlocal cancel_calls
        cancel_calls += 1
        cancel_entered.set()
        await release_cancel.wait()
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                             ok=True, payload={"success": True})

    manager.agent.process_message = cancel_agent
    def envelope(request_id, disconnected):
        env = e2a_from_agent_fields(
            request_id=request_id, channel_id="tui", session_id=sid,
            req_method=ReqMethod.CHAT_CANCEL, params={"intent": "cancel", "session_id": sid},
            is_stream=False, timestamp=0.0,
        )
        if disconnected:
            env.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"
        return json.dumps(env.to_dict())

    manual_ws, disconnect_ws = FakeWebSocket(), FakeWebSocket()
    manual = asyncio.create_task(server.handle_message_for_test(manual_ws, envelope("manual", False), asyncio.Lock()))
    disconnected = None
    try:
        await asyncio.wait_for(cancel_entered.wait(), 1)
        disconnected = asyncio.create_task(server.handle_message_for_test(
            disconnect_ws, envelope("disconnect", True), asyncio.Lock()))
        await asyncio.sleep(0)
        release_cancel.set()
        await asyncio.wait_for(manager.cleanup_started.wait(), 1)
        await asyncio.wait_for(manual, 1)
        done, _ = await asyncio.wait({disconnected}, timeout=0.2)
        assert disconnected in done
        assert manual_ws.sent[-1]["status"] == "succeeded"
        assert disconnect_ws.sent[-1]["status"] == "failed"
        assert cancel_calls == 1
        manager.allow_cleanup.set()
        await asyncio.gather(*list(server._stream_shutdown_jobs.values()))
        assert manager.cleaned == [("tui", sid)]
    finally:
        release_cancel.set()
        manager.allow_cleanup.set()
        await asyncio.gather(*[task for task in (manual, disconnected) if task is not None], return_exceptions=True)
        await asyncio.gather(*list(getattr(server, "_stream_shutdown_jobs", {}).values()), return_exceptions=True)
