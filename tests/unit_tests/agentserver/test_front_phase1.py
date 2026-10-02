# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import sys

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.front.router import CONTROL_METHODS, MethodRouter, is_control_method
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState


def _request(method: ReqMethod, **kwargs) -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        channel_id="web",
        req_method=method,
        params={},
        **kwargs,
    )


def test_control_methods_include_queries_not_session_create() -> None:
    assert ReqMethod.SESSION_LIST.value in CONTROL_METHODS
    assert ReqMethod.SESSION_GET_METADATA.value in CONTROL_METHODS
    assert ReqMethod.PROJECT_LIST.value in CONTROL_METHODS
    assert ReqMethod.CONFIG_GET.value in CONTROL_METHODS
    assert ReqMethod.HISTORY_GET.value in CONTROL_METHODS
    assert ReqMethod.HEALTH_CHECK_GET_CONF.value in CONTROL_METHODS
    # Phase 1 exception to proposal §6.1: create/switch/delete stay on execution.
    assert ReqMethod.SESSION_CREATE.value not in CONTROL_METHODS
    assert ReqMethod.SESSION_SWITCH.value not in CONTROL_METHODS
    assert ReqMethod.SESSION_DELETE.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_CREATE.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_RENAME.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_PIN.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_GIT_COMMIT.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_GIT_PUSH.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_GIT_INIT.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_GIT_STATUS.value not in CONTROL_METHODS
    assert ReqMethod.PROJECT_GIT_DIFF_STATUS.value not in CONTROL_METHODS
    assert ReqMethod.CHAT_SEND.value not in CONTROL_METHODS
    assert ReqMethod.CONFIG_VALIDATE_MODEL.value not in CONTROL_METHODS
    assert ReqMethod.COMMAND_MODEL.value not in CONTROL_METHODS
    assert ReqMethod.SESSION_PLAN_STATUS.value not in CONTROL_METHODS


@pytest.mark.parametrize(
    "method, expected",
    [
        (ReqMethod.SESSION_LIST, True),
        (ReqMethod.CONFIG_GET, True),
        (ReqMethod.HISTORY_GET, True),
        (ReqMethod.HEALTH_CHECK_GET_CONF, True),
        (ReqMethod.SESSION_CREATE, False),
        (ReqMethod.PROJECT_CREATE, False),
        (ReqMethod.PROJECT_GIT_COMMIT, False),
        (ReqMethod.PROJECT_GIT_STATUS, False),
        (ReqMethod.CHAT_SEND, False),
    ],
)
def test_is_control_method(method: ReqMethod, expected: bool) -> None:
    assert is_control_method(_request(method)) is expected


def test_readiness_advances_in_order() -> None:
    readiness = Readiness()
    assert readiness.state is ReadinessState.STARTING
    readiness.mark_transport_ready()
    assert readiness.snapshot()["transport_ready"] is True
    readiness.mark_control_ready()
    assert readiness.snapshot()["control_ready"] is True
    readiness.mark_runtime_warming()
    readiness.mark_agent_ready()
    assert readiness.snapshot()["agent_ready"] is True
    assert ReadinessState.TRANSPORT_READY.value == "TRANSPORT_READY"


@pytest.mark.asyncio
async def test_router_sends_session_create_to_admission() -> None:
    recorded: list[ReqMethod | None] = []

    class _FakeAdmission:
        async def dispatch(self, ws, request, send_lock) -> None:
            _ = ws, send_lock
            recorded.append(request.req_method)

    router = MethodRouter(Readiness(), _FakeAdmission())  # type: ignore[arg-type]
    await router.dispatch(object(), _request(ReqMethod.SESSION_CREATE), object())
    assert recorded == [ReqMethod.SESSION_CREATE]


@pytest.mark.asyncio
async def test_router_sends_project_create_to_admission() -> None:
    recorded: list[ReqMethod | None] = []

    class _FakeAdmission:
        async def dispatch(self, ws, request, send_lock) -> None:
            _ = ws, send_lock
            recorded.append(request.req_method)

    router = MethodRouter(Readiness(), _FakeAdmission())  # type: ignore[arg-type]
    await router.dispatch(object(), _request(ReqMethod.PROJECT_CREATE), object())
    assert recorded == [ReqMethod.PROJECT_CREATE]


@pytest.mark.asyncio
async def test_router_sends_project_git_status_to_admission() -> None:
    recorded: list[ReqMethod | None] = []

    class _FakeAdmission:
        async def dispatch(self, ws, request, send_lock) -> None:
            _ = ws, send_lock
            recorded.append(request.req_method)

    router = MethodRouter(Readiness(), _FakeAdmission())  # type: ignore[arg-type]
    await router.dispatch(object(), _request(ReqMethod.PROJECT_GIT_STATUS), object())
    assert recorded == [ReqMethod.PROJECT_GIT_STATUS]


@pytest.mark.asyncio
async def test_admission_rejects_when_runtime_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = Readiness()
    admission = ExecutionAdmission(readiness, wait_seconds=0.01)
    sent: list[object] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.front.admission.send_wire_payload",
        _send,
    )
    readiness.mark_failed("boot failed")
    await admission.dispatch(object(), _request(ReqMethod.CHAT_SEND), asyncio.Lock())
    assert sent
    assert "RUNTIME_FAILED" in str(sent[0])


@pytest.mark.asyncio
async def test_queue_overflow_reject_does_not_hold_admission_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_started = asyncio.Event()
    send_release = asyncio.Event()

    async def _send(ws, payload) -> bool:
        _ = ws, payload
        send_started.set()
        await send_release.wait()
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.front.admission.send_wire_payload",
        _send,
    )

    class _FakeBackend:
        dispatched = 0

        async def dispatch_parsed_request(self, ws, request, send_lock) -> None:
            _ = ws, request, send_lock
            self.dispatched += 1

        def attach_gateway_connection(self, ws, send_lock) -> None:
            return None

        async def on_gateway_disconnect(self, ws, remote) -> None:
            return None

    readiness = Readiness()
    admission = ExecutionAdmission(readiness, queue_limit=1, wait_seconds=5.0)
    backend = _FakeBackend()
    req = _request(ReqMethod.CHAT_SEND)

    waiter = asyncio.create_task(admission.dispatch(object(), req, asyncio.Lock()))
    for _ in range(50):
        if admission._waiting == 1:
            break
        await asyncio.sleep(0)
    assert admission._waiting == 1

    rejecter = asyncio.create_task(admission.dispatch(object(), req, asyncio.Lock()))
    await asyncio.wait_for(send_started.wait(), 1.0)

    admission.attach_backend(backend)
    readiness.mark_agent_ready()
    await asyncio.wait_for(waiter, 1.0)
    assert backend.dispatched == 1
    assert admission._waiting == 0

    send_release.set()
    await asyncio.wait_for(rejecter, 1.0)


def test_front_module_import_does_not_load_runtime() -> None:
    """Importing Front in a clean interpreter must not load OpenJiuwen or AgentWS."""
    import subprocess

    script = r"""
import os
import sys
os.environ["JIUWENSWARM_RUNTIME_WORKSPACE_READY"] = "1"
forbidden = (
    "openjiuwen",
    "jiuwenswarm.server.agent_ws_server",
    "jiuwenswarm.gateway.channel_manager.web.app_web_handlers",
    "jiuwenswarm.agents.harness",
    "jiuwenswarm.common.config",
    "jiuwenswarm.server.runtime.gateway_adapter",
    "jiuwenswarm.server.runtime.a2ui",
)
import jiuwenswarm.server.app_agentserver  # noqa: F401
hits = [
    name for name in sys.modules
    if name in forbidden or any(
        name == prefix or name.startswith(prefix + ".") for prefix in forbidden
    )
]
if hits:
    raise SystemExit("unexpected modules after app_agentserver: " + ",".join(sorted(hits)[:20]))
import jiuwenswarm.server.front.server  # noqa: F401
hits = [
    name for name in sys.modules
    if name in forbidden or any(
        name == prefix or name.startswith(prefix + ".") for prefix in forbidden
    )
]
if hits:
    raise SystemExit("unexpected modules after front.server: " + ",".join(sorted(hits)[:20]))
import jiuwenswarm.server.control.config_service  # noqa: F401
import jiuwenswarm.server.control.session_service  # noqa: F401
import jiuwenswarm.server.control.project_service  # noqa: F401
hits = [
    name for name in sys.modules
    if name in forbidden or any(
        name == prefix or name.startswith(prefix + ".") for prefix in forbidden
    )
]
if hits:
    raise SystemExit("unexpected modules after control services: " + ",".join(sorted(hits)[:20]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


class _HoldCloseWs:
    def __init__(self, name: str, close_gate: asyncio.Event) -> None:
        self.remote_address = name
        self._close_gate = close_gate
        self._started = False

    def __aiter__(self) -> "_HoldCloseWs":
        return self

    async def __anext__(self) -> str:
        if not self._started:
            self._started = True
            await self._close_gate.wait()
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_clear_newer_gateway_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _send(ws, payload) -> bool:
        _ = ws, payload
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.front.connection.send_wire_payload",
        _send,
    )
    from jiuwenswarm.server.front.admission import ExecutionAdmission
    from jiuwenswarm.server.front.connection import ConnectionHandler
    from jiuwenswarm.server.front.router import MethodRouter

    class _FakeBackend:
        def __init__(self) -> None:
            self.disconnected: list[object] = []

        def attach_gateway_connection(self, ws: object, send_lock: asyncio.Lock) -> None:
            _ = send_lock
            return None

        async def dispatch_parsed_request(self, ws, request, send_lock) -> None:
            _ = ws, request, send_lock

        async def on_gateway_disconnect(self, ws: object, remote: object) -> None:
            _ = remote
            self.disconnected.append(ws)

    readiness = Readiness()
    admission = ExecutionAdmission(readiness)
    backend = _FakeBackend()
    admission.attach_backend(backend)
    handler = ConnectionHandler(
        readiness,
        MethodRouter(readiness, admission),
        admission,
    )
    close_a = asyncio.Event()
    close_b = asyncio.Event()
    ws_a = _HoldCloseWs("A", close_a)
    ws_b = _HoldCloseWs("B", close_b)

    async def _wait_current(expected: object) -> None:
        for _ in range(50):
            if handler.current_ws is expected:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"current_ws is {handler.current_ws!r}, expected {expected!r}")

    task_a = asyncio.create_task(handler(ws_a))
    await _wait_current(ws_a)

    task_b = asyncio.create_task(handler(ws_b))
    await _wait_current(ws_b)

    close_a.set()
    await task_a
    assert handler.current_ws is ws_b
    assert handler.current_send_lock is not None
    assert backend.disconnected == []

    close_b.set()
    await task_b
    assert handler.current_ws is None
    assert backend.disconnected == [ws_b]


def _history_stream_request(**params) -> AgentRequest:
    return AgentRequest(
        request_id="req-hist",
        channel_id="web",
        req_method=ReqMethod.HISTORY_GET,
        params=params,
        is_stream=True,
    )


@pytest.mark.asyncio
async def test_history_stream_accepts_web_cursor_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_chunk",
        lambda chunk, *, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )
    from jiuwenswarm.server.front.router import MethodRouter, _load_control_services

    _load_control_services()
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._LOAD_HISTORY_QUERY",
        lambda _params: {
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "next_cursor": None,
            "has_more": False,
            "snapshot_id": "snap-1",
            "snapshot_end": 42,
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._LOAD_HISTORY_TODO_SNAPSHOT",
        lambda _session_id: [{"id": "t1", "content": "todo", "activeForm": "todo", "status": "pending"}],
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router._STREAM_HISTORY_RECORDS",
        lambda messages, *, use_split: messages,
    )

    router = MethodRouter(Readiness(), ExecutionAdmission(Readiness()))
    await router.dispatch(
        object(),
        _history_stream_request(session_id="web_abc", cursor=None, limit=50),
        asyncio.Lock(),
    )

    payloads = [item["payload"] for item in sent]
    event_types = [payload.get("event_type") for payload in payloads]
    assert "chat.error" not in event_types
    assert payloads[0]["event_type"] == "history.message"
    assert payloads[0]["message"]["content"] == "hi"
    assert payloads[0]["cursor"] is None
    assert payloads[0]["has_more"] is False
    assert payloads[0]["snapshot_id"] == "snap-1"
    assert "todo.updated" in event_types
    assert payloads[-1]["status"] == "done"
    assert payloads[-1]["event_type"] == "history.message"


@pytest.mark.asyncio
async def test_history_stream_reports_invalid_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.control.history_service import InvalidHistoryCursor
    from jiuwenswarm.server.front.router import MethodRouter, _load_control_services

    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_chunk",
        lambda chunk, *, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )
    _load_control_services()

    def _raise(_params):
        raise InvalidHistoryCursor("history cursor must be null or a string")

    monkeypatch.setattr("jiuwenswarm.server.front.router._LOAD_HISTORY_QUERY", _raise)
    router = MethodRouter(Readiness(), ExecutionAdmission(Readiness()))
    await router.dispatch(
        object(),
        _history_stream_request(session_id="web_abc", cursor=123, limit=50),
        asyncio.Lock(),
    )

    assert len(sent) == 1
    assert sent[0]["payload"]["event_type"] == "history.message"
    assert sent[0]["payload"]["status"] == "error"
    assert sent[0]["payload"]["code"] == "INVALID_HISTORY_CURSOR"


class _EmptyWs:
    remote_address = "test"

    def __aiter__(self) -> "_EmptyWs":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_handshake_ack_completes_while_runtime_thread_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    sent: list[tuple[float, dict]] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append((time.monotonic(), payload))
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.connection.send_wire_payload", _send)
    from jiuwenswarm.server.front.connection import ConnectionHandler

    readiness = Readiness()
    readiness.mark_transport_ready()
    readiness.mark_control_ready()
    readiness.mark_runtime_warming()
    handler = ConnectionHandler(
        readiness,
        MethodRouter(readiness, ExecutionAdmission(readiness)),
        ExecutionAdmission(readiness),
    )

    async def _block() -> None:
        await asyncio.to_thread(time.sleep, 1.0)

    t0 = time.monotonic()
    await asyncio.gather(handler(_EmptyWs()), _block())
    assert sent
    ack = sent[0][1]
    assert ack["event"] == "connection.ack"
    assert ack["payload"]["status"] == "ready"
    assert ack["payload"]["readiness"] == "RUNTIME_WARMING"
    assert sent[0][0] - t0 < 0.3


@pytest.mark.asyncio
async def test_health_returns_warming_without_runtime_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_response",
        lambda response, *, response_id: {
            "response_id": response_id,
            "payload": response.payload,
            "ok": response.ok,
        },
    )
    readiness = Readiness()
    readiness.mark_transport_ready()
    readiness.mark_control_ready()
    readiness.mark_runtime_warming()
    router = MethodRouter(readiness, ExecutionAdmission(readiness))
    await router.dispatch(
        object(),
        _request(ReqMethod.HEALTH_CHECK_GET_CONF),
        asyncio.Lock(),
    )
    assert sent
    payload = sent[0]["payload"]
    assert payload["state"] == "RUNTIME_WARMING"
    assert payload["ok"] is True
    assert payload["agent_ready"] is False
    assert payload["control_ready"] is True
    assert payload["capabilities"]["execution"] is False


@pytest.mark.asyncio
async def test_health_returns_failed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_response",
        lambda response, *, response_id: {
            "response_id": response_id,
            "payload": response.payload,
            "ok": response.ok,
        },
    )
    readiness = Readiness()
    readiness.mark_failed("openjiuwen import failed")
    router = MethodRouter(readiness, ExecutionAdmission(readiness))
    await router.dispatch(
        object(),
        _request(ReqMethod.HEALTH_CHECK_GET_CONF),
        asyncio.Lock(),
    )
    payload = sent[0]["payload"]
    assert payload["state"] == "FAILED"
    assert payload["failed"] is True
    assert payload["failed_reason"] == "openjiuwen import failed"
    assert payload["last_error"] == "openjiuwen import failed"
    assert payload["retrying"] is False
    assert payload["agent_ready"] is False


def test_warmup_retry_stays_warming_not_failed() -> None:
    readiness = Readiness()
    readiness.mark_runtime_warming()
    readiness.note_warmup_retry("checkpointer down")
    snap = readiness.snapshot()
    assert snap["state"] == "RUNTIME_WARMING"
    assert snap["failed"] is False
    assert snap["failed_reason"] is None
    assert snap["retrying"] is True
    assert snap["last_error"] == "checkpointer down"
    assert snap["agent_ready"] is False
    assert snap["control_ready"] is True


@pytest.mark.asyncio
async def test_warmup_retry_does_not_release_wait_agent_ready() -> None:
    readiness = Readiness()
    readiness.mark_runtime_warming()
    readiness.note_warmup_retry("boom")
    assert await readiness.wait_agent_ready(timeout=0.05) is False
    readiness.mark_agent_ready()
    assert await readiness.wait_agent_ready(timeout=0.05) is True
    snap = readiness.snapshot()
    assert snap["retrying"] is False
    assert snap["last_error"] is None
    assert snap["state"] == "AGENT_READY"


@pytest.mark.asyncio
async def test_health_returns_warmup_retry_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", _send)
    monkeypatch.setattr(
        "jiuwenswarm.server.front.router.encode_response",
        lambda response, *, response_id: {
            "response_id": response_id,
            "payload": response.payload,
            "ok": response.ok,
        },
    )
    readiness = Readiness()
    readiness.mark_transport_ready()
    readiness.mark_control_ready()
    readiness.mark_runtime_warming()
    readiness.note_warmup_retry("openjiuwen import failed")
    router = MethodRouter(readiness, ExecutionAdmission(readiness))
    await router.dispatch(
        object(),
        _request(ReqMethod.HEALTH_CHECK_GET_CONF),
        asyncio.Lock(),
    )
    payload = sent[0]["payload"]
    assert payload["state"] == "RUNTIME_WARMING"
    assert payload["failed"] is False
    assert payload["retrying"] is True
    assert payload["last_error"] == "openjiuwen import failed"
    assert payload["control_ready"] is True
    assert payload["agent_ready"] is False


def test_attach_backend_does_not_mark_agent_ready() -> None:
    class _FakeBackend:
        async def dispatch_parsed_request(self, ws, request, send_lock) -> None:
            _ = ws, request, send_lock

        def attach_gateway_connection(self, ws, send_lock) -> None:
            return None

        async def on_gateway_disconnect(self, ws, remote) -> None:
            return None

    readiness = Readiness()
    readiness.mark_runtime_warming()
    admission = ExecutionAdmission(readiness)
    admission.attach_backend(_FakeBackend())
    assert readiness.state is ReadinessState.RUNTIME_WARMING
    assert readiness.snapshot()["agent_ready"] is False


@pytest.mark.asyncio
async def test_drain_rejects_new_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[object] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.admission.send_wire_payload", _send)
    readiness = Readiness()
    readiness.mark_runtime_warming()
    admission = ExecutionAdmission(readiness)
    readiness.mark_draining()
    await admission.dispatch(object(), _request(ReqMethod.CHAT_SEND), asyncio.Lock())
    assert "RUNTIME_DRAINING" in str(sent[0])


def test_connection_ack_failed_is_not_status_ready() -> None:
    from jiuwenswarm.server.front.protocol import connection_ack_frame

    frame = connection_ack_frame(readiness_state="FAILED")
    assert frame["payload"]["status"] == "failed"
    assert frame["payload"]["readiness"] == "FAILED"


def test_readiness_order_does_not_skip_warming() -> None:
    readiness = Readiness()
    readiness.mark_transport_ready()
    readiness.mark_control_ready()
    readiness.mark_runtime_warming()
    assert readiness.state is ReadinessState.RUNTIME_WARMING
    readiness.mark_agent_ready()
    assert readiness.state is ReadinessState.AGENT_READY


def test_runtime_start_defaults_to_no_transport_bind() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    text = (root / "jiuwenswarm/server/agent_ws_server.py").read_text(encoding="utf-8")
    assert "async def start(self, *, bind_transport: bool = False)" in text


def test_runtime_construct_is_off_front_loop() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    text = (root / "jiuwenswarm/server/app_agentserver.py").read_text(encoding="utf-8")
    assert "server = await asyncio.to_thread(_construct_runtime_server)" in text
    assert "AgentWebSocketServer.get_instance(host=host, port=port)" in text


@pytest.mark.asyncio
async def test_front_listen_marks_control_ready_within_budget() -> None:
    import socket
    import time

    from jiuwenswarm.server.front.server import AgentServerFront

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    front = AgentServerFront(host="127.0.0.1", port=port)
    t0 = time.monotonic()
    await front.start()
    elapsed = time.monotonic() - t0
    try:
        snapshot = front.readiness.snapshot()
        assert elapsed < 0.3
        assert snapshot["transport_ready"] is True
        assert snapshot["control_ready"] is True
        assert snapshot["agent_ready"] is False
    finally:
        await front.stop()


@pytest.mark.asyncio
async def test_chat_cancel_drops_queued_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.admission.send_wire_payload", _send)
    readiness = Readiness()
    readiness.mark_runtime_warming()
    admission = ExecutionAdmission(readiness, wait_seconds=5.0)
    chat = _request(ReqMethod.CHAT_SEND)
    chat.session_id = "sess-1"
    waiter = asyncio.create_task(admission.dispatch(object(), chat, asyncio.Lock()))
    for _ in range(50):
        if admission._waiting == 1:
            break
        await asyncio.sleep(0)
    assert admission._waiting == 1
    cancel = _request(ReqMethod.CHAT_CANCEL)
    cancel.session_id = "sess-1"
    await admission.dispatch(object(), cancel, asyncio.Lock())
    await asyncio.wait_for(waiter, 1.0)
    joined = " ".join(str(item) for item in sent)
    assert "REQUEST_CANCELLED" in joined
    assert "chat.interrupt_result" in joined


@pytest.mark.asyncio
async def test_queued_stream_emits_runtime_warming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []

    async def _send(ws, payload) -> bool:
        _ = ws
        sent.append(payload)
        return True

    monkeypatch.setattr("jiuwenswarm.server.front.admission.send_wire_payload", _send)
    readiness = Readiness()
    readiness.mark_runtime_warming()
    admission = ExecutionAdmission(readiness, wait_seconds=5.0)
    chat = _request(ReqMethod.CHAT_SEND, is_stream=True)
    chat.session_id = "sess-warm"
    waiter = asyncio.create_task(admission.dispatch(object(), chat, asyncio.Lock()))
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0)
    cancel = _request(ReqMethod.CHAT_CANCEL)
    cancel.session_id = "sess-warm"
    await admission.dispatch(object(), cancel, asyncio.Lock())
    await asyncio.wait_for(waiter, 1.0)
    assert any("runtime.warming" in str(item) for item in sent)
