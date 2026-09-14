# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for connection-close scoping: per-request cancel vs shutdown global cancel."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


class FakeTransport:
    """MessageTransport double: scripted recv_text, recorded send_text/close."""

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.sent: list[str] = []
        self.closed = False

    async def recv_text(self) -> str | None:
        # 让出事件循环：真实 WS recv 有 I/O 挂起，消息 task 在连接关闭前有机会执行
        await asyncio.sleep(0)
        if self._payloads:
            return self._payloads.pop(0)
        return None

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _ScopedRecordingAgentManager:
    def __init__(self) -> None:
        self.global_cancel_calls: list[str] = []
        self.scoped_cancel_calls: list[list[tuple[str, str]]] = []

    async def cancel_all_inflight_work(self, reason: str = "") -> None:
        self.global_cancel_calls.append(reason)

    async def cancel_inflight_requests(
        self, requests: list[tuple[str, str]], reason: str = ""
    ) -> None:
        self.scoped_cancel_calls.append(list(requests))


def _make_server(manager: _ScopedRecordingAgentManager, *, stopping: bool) -> AgentWebSocketServer:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager  # pylint: disable=protected-access
    server._stopping = stopping  # pylint: disable=protected-access
    server._session_stream_tasks = {}  # pylint: disable=protected-access
    server._acp_client_capabilities_by_ws = {}  # pylint: disable=protected-access
    server._scheduler_service = None  # pylint: disable=protected-access
    return server


@pytest.fixture()
def _rpc_fail_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize connection-teardown RPC fail_all singletons."""
    for name in (
        "get_device_command_manager",
        "get_gui_rpc_client",
        "get_reverse_rpc_client",
    ):
        fake = MagicMock()
        fake.fail_all = MagicMock()
        monkeypatch.setattr(agent_ws_server_module, name, lambda _fake=fake: _fake)


def _chat_send_env(request_id: str, session_id: str) -> str:
    env = e2a_from_agent_fields(
        request_id=request_id,
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
        is_stream=True,
        timestamp=0.0,
    )
    return json.dumps(env.to_dict(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_connection_close_cancels_only_own_requests(
    monkeypatch: pytest.MonkeyPatch, _rpc_fail_all: None
) -> None:
    """Non-shutdown close: scoped per-request cancel, never the global kill."""
    server = _make_server(_ScopedRecordingAgentManager(), stopping=False)
    registered: dict[str, str] = {}

    async def stub_handle_message(ws, raw, send_lock, conn_inflight=None) -> None:
        if conn_inflight is not None:
            conn_inflight["req-1"] = "sess-1"
            registered.update(conn_inflight)

    server._handle_message = stub_handle_message  # pylint: disable=protected-access
    transport = FakeTransport([_chat_send_env("req-1", "sess-1")])

    await server.run_connection(transport, remote=("127.0.0.1", 1))  # pylint: disable=protected-access

    assert registered == {"req-1": "sess-1"}
    manager = server._agent_manager  # pylint: disable=protected-access
    assert manager.global_cancel_calls == []
    assert manager.scoped_cancel_calls == [[("sess-1", "req-1")]]
    assert transport.closed is True


@pytest.mark.asyncio
async def test_shutdown_close_keeps_global_cancel(
    monkeypatch: pytest.MonkeyPatch, _rpc_fail_all: None
) -> None:
    """Server shutdown: global kill stays (original gateway-exclusive semantics)."""
    server = _make_server(_ScopedRecordingAgentManager(), stopping=True)

    async def stub_handle_message(ws, raw, send_lock, conn_inflight=None) -> None:
        return None

    server._handle_message = stub_handle_message  # pylint: disable=protected-access
    transport = FakeTransport([_chat_send_env("req-1", "sess-1")])

    await server.run_connection(transport, remote=("127.0.0.1", 1))  # pylint: disable=protected-access

    manager = server._agent_manager  # pylint: disable=protected-access
    assert len(manager.global_cancel_calls) == 1
    assert manager.scoped_cancel_calls == []


@pytest.mark.asyncio
async def test_connection_close_without_chat_send_skips_cancel(
    monkeypatch: pytest.MonkeyPatch, _rpc_fail_all: None
) -> None:
    """A connection that never sent chat.send must not trigger any cancel."""
    server = _make_server(_ScopedRecordingAgentManager(), stopping=False)

    async def stub_handle_message(ws, raw, send_lock, conn_inflight=None) -> None:
        return None

    server._handle_message = stub_handle_message  # pylint: disable=protected-access
    transport = FakeTransport([])

    await server.run_connection(transport, remote=("127.0.0.1", 1))  # pylint: disable=protected-access

    manager = server._agent_manager  # pylint: disable=protected-access
    assert manager.global_cancel_calls == []
    assert manager.scoped_cancel_calls == []


@pytest.mark.asyncio
async def test_handle_message_registers_chat_send_into_conn_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real _handle_message must register chat.send request into conn_inflight."""
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._acp_client_capabilities_by_ws = {}  # pylint: disable=protected-access
    conn_inflight: dict[str, str] = {}
    server._handle_stream = AsyncMock()  # pylint: disable=protected-access

    # 注册发生在分发之前；分发脚手架未完整 mock，后续步骤的异常与本用例无关
    try:
        await server._handle_message(  # pylint: disable=protected-access
            FakeTransport([]),
            _chat_send_env("req-reg", "sess-reg"),
            asyncio.Lock(),
            conn_inflight,
        )
    except Exception:  # noqa: BLE001 - 分发链路的脚手架噪声
        pass

    assert conn_inflight == {"req-reg": "sess-reg"}
