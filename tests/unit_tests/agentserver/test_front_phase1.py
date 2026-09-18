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
    assert ReqMethod.SESSION_CREATE.value not in CONTROL_METHODS
    assert ReqMethod.SESSION_SWITCH.value not in CONTROL_METHODS
    assert ReqMethod.SESSION_DELETE.value not in CONTROL_METHODS
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
        (ReqMethod.SESSION_CREATE, False),
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
    assert "RUNTIME_WARMING" in str(sent[0])


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
hits = [
    name for name in sys.modules
    if name in forbidden or any(
        name == prefix or name.startswith(prefix + ".") for prefix in forbidden
    )
]
if hits:
    raise SystemExit("unexpected modules after config_service: " + ",".join(sorted(hits)[:20]))
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
