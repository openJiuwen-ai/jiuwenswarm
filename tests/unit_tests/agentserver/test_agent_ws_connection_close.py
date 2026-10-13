import asyncio
import json
import logging
import weakref
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.handlers import _default
from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.common.e2a.gateway_normalize import (
    build_fallback_e2a,
    e2a_from_agent_fields,
)
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer, _process_snapshot
from jiuwenswarm.server.context import AgentServerServices as _services
from jiuwenswarm.server.handlers import schedule as schedule_handlers


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
    """连接在非流式处理途中断开。

    默认路径已下沉为 ``handlers/_default`` 的模块级函数，``pipeline`` 在模块级
    import 它 —— 因此**不能再靠子类覆盖方法**来模拟，桩要打在 ``pipeline``
    引用的那个名字上（见 :func:`_closed_during_unary`）。
    """


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
    # 断开诊断日志现在由 server/pipeline 打（汇合点搬过去了）—— 消息内容不变，
    # 但 logger 名跟着模块走，所以两个都要挂上。
    target_logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")
    pipeline_logger = logging.getLogger("jiuwenswarm.server.pipeline")
    for _lg in (target_logger, pipeline_logger):
        _lg.addHandler(caplog.handler)
        caplog.set_level(logging.INFO, logger=_lg.name)
    env = e2a_from_agent_fields(
        request_id="req-closed",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CONFIG_GET,
        params={},
        is_stream=False,
        timestamp=0.0,
    )
    async def _closed_during_unary(ctx, request) -> None:  # noqa: ANN001
        raise ConnectionClosedError(None, None)

    from jiuwenswarm.server import pipeline as pipeline_module

    original = pipeline_module._handle_unary
    pipeline_module._handle_unary = _closed_during_unary
    try:
        await ClosedDuringUnaryServer().handle_message_for_test(
            FakeWebSocket(),
            json.dumps(env.to_dict(), ensure_ascii=False),
            asyncio.Lock(),
        )
    finally:
        pipeline_module._handle_unary = original
        for _lg in (target_logger, pipeline_logger):
            _lg.removeHandler(caplog.handler)

    assert "no close frame received or sent" in caplog.text
    assert "request_id=req-closed" in caplog.text


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

    # 断言 json_error 诊断字段而非日志正文里的 "JSON" 二字：解析段抽到
    # server/wire_parse.py 后，这条日志同时覆盖 JSON 解码失败与未知方法两种，
    # 文案泛化成了「解析错误未发送」；具体是哪一种由 log_context 带出来。
    assert "json_error=" in caplog.text


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
    await manager.cleanup_started.wait()

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
        "error": "session runtime cleanup failed",
    }


def test_session_mode_sync_lock_cache_does_not_retain_idle_sessions() -> None:
    session_id = "sess-weak-lock"
    lock = _default._session_mode_sync_lock(session_id)
    lock_ref = weakref.ref(lock)

    assert agent_ws_server_module._session_mode_sync_locks.get(session_id) is lock

    del lock

    assert lock_ref() is None
    assert session_id not in agent_ws_server_module._session_mode_sync_locks


def test_scheduler_agent_pin_moves_with_persistent_owner() -> None:
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    manager = _PinRecordingAgentManager()
    first = object()
    second = object()
    server._agent_manager = manager
    server._scheduler_agent = None

    # 接收者由隐式self变成显式ctx，这里用最小ctx复现生产调用形态。
    ctx = SimpleNamespace(services=_services(server))
    schedule_handlers._set_scheduler_agent(ctx, first)
    schedule_handlers._set_scheduler_agent(ctx, second)
    schedule_handlers._set_scheduler_agent(ctx, second)

    assert manager.pinned == [first, second]
    assert manager.unpinned == [first]
    assert server._scheduler_agent is second


@pytest.mark.asyncio
async def test_disconnect_cancel_does_not_create_agent_when_runtime_missing() -> None:
    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
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
    async def failing_stream_task() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            raise RuntimeError("stream cleanup failed")

    server = _AgentWsTestHarness.__new__(_AgentWsTestHarness)
    manager = _CleanupRecordingAgentManager()
    server._agent_manager = manager
    stream_task = asyncio.create_task(failing_stream_task())
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


class _AbortingWebSocket:
    """对端不发 close 帧就消失，对应日志里的 1006。"""

    remote_address = ("127.0.0.1", 52095)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ConnectionClosedError(None, None)

    async def send(self, _payload: str) -> None:
        return None


async def _async_noop(*_args, **_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_abrupt_disconnect_cancels_round_and_stops_scheduler(
    monkeypatch, caplog,
) -> None:
    target_logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")
    target_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=target_logger.name)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._trigger_agent_server_started_hook = _async_noop
    server._ping_interval = 30
    server._ping_timeout = 300
    server._server = None
    server._session_stream_tasks = {}
    server._acp_client_capabilities_by_ws = {}
    cancelled: list[str] = []
    scheduler_stops: list[str] = []

    async def cancel_all(reason: str = "") -> None:
        cancelled.append(reason)

    async def stop_scheduler() -> None:
        scheduler_stops.append("stop")

    server._agent_manager = SimpleNamespace(cancel_all_inflight_work=cancel_all)
    server._stop_scheduler = stop_scheduler
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.cancel_all_team_stream_tasks_across_managers",
        _async_noop,
    )

    try:
        await server._connection_handler(_AbortingWebSocket())
    finally:
        target_logger.removeHandler(caplog.handler)

    assert len(cancelled) == 1
    assert "127.0.0.1" in cancelled[0]
    assert scheduler_stops == ["stop"]
    assert "step='stop_scheduler' phase='done'" in caplog.text
    assert "连接关闭" in caplog.text
    assert "进程快照" in caplog.text
    assert "rss_mb=" in caplog.text
    assert "threads=" in caplog.text
    assert "handles=" in caplog.text
    assert "sockets=" in caplog.text
    assert "step='cancel_inflight' phase='start'" in caplog.text
    assert "step='cancel_inflight' phase='done'" in caplog.text
    assert "step='team_stream' phase='start'" in caplog.text
    assert "step='team_stream' phase='done'" in caplog.text
    assert "step='join_tasks' phase='done'" in caplog.text
    assert "连接清理完成" in caplog.text
    assert "listener_present" in caplog.text
    assert "pid=" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_step_error_still_reaches_later_steps(
    monkeypatch, caplog,
) -> None:
    target_logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")
    target_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=target_logger.name)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._trigger_agent_server_started_hook = _async_noop
    server._ping_interval = 30
    server._ping_timeout = 300
    server._server = None
    server._session_stream_tasks = {}
    server._acp_client_capabilities_by_ws = {}

    async def cancel_all(reason: str = "") -> None:
        raise RuntimeError(reason)

    server._agent_manager = SimpleNamespace(cancel_all_inflight_work=cancel_all)
    server._stop_scheduler = _async_noop
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.cancel_all_team_stream_tasks_across_managers",
        _async_noop,
    )

    try:
        await server._connection_handler(_AbortingWebSocket())
    finally:
        target_logger.removeHandler(caplog.handler)

    assert "step='cancel_inflight' phase='error'" in caplog.text
    assert "step='team_stream' phase='done'" in caplog.text
    assert "连接清理完成" in caplog.text


def test_process_snapshot_keeps_other_fields_when_one_probe_fails(monkeypatch) -> None:
    def _boom(_proc: object) -> float:
        raise RuntimeError("rss")

    monkeypatch.setattr(agent_ws_server_module, "_snapshot_rss_mb", _boom)
    snapshot = _process_snapshot()
    assert snapshot["rss_mb"] == "unavailable"
    assert snapshot["threads"] != "unavailable"
    assert "handles" in snapshot
    assert "sockets" in snapshot
