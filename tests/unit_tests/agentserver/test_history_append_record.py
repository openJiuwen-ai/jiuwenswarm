"""Regression coverage for the cron failure-history append RPC."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module


class _FakeWebSocket:
    pass


@pytest.mark.asyncio
async def test_history_append_record_persists_and_waits_for_completion(monkeypatch) -> None:
    appended: dict = {}
    sent: list[dict] = []
    receipt: Future[None] = Future()
    receipt.set_result(None)

    monkeypatch.setattr(
        agent_ws_server_module,
        "append_history_record",
        lambda **kwargs: appended.update(kwargs),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *_args, **_kwargs: receipt,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda response, *, response_id: {
            "response_id": response_id,
            "ok": response.ok,
            "payload": response.payload,
        },
    )

    async def _capture_send(_ws, wire):
        sent.append(wire)

    monkeypatch.setattr(agent_ws_server_module, "send_wire_payload", _capture_send)
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    request = AgentRequest(
        request_id="rpc-1",
        channel_id="__cron__",
        session_id="cron-session",
        req_method=ReqMethod.HISTORY_APPEND_RECORD,
        timestamp=123.5,
        params={
            "request_id": "cron-request-1",
            "channel_id": "web",
            "role": "assistant",
            "event_type": "chat.final",
            "mode": "deep",
            "content": "[cron] task failed",
        },
    )

    await server._handle_history_append_record(_FakeWebSocket(), request, asyncio.Lock())

    assert appended == {
        "session_id": "cron-session",
        "request_id": "cron-request-1",
        "channel_id": "web",
        "role": "assistant",
        "content": "[cron] task failed",
        "timestamp": 123.5,
        "event_type": "chat.final",
        "mode": "deep",
    }
    assert sent == [
        {
            "response_id": "rpc-1",
            "ok": True,
            "payload": {"persisted": True, "session_id": "cron-session"},
        }
    ]


@pytest.mark.asyncio
async def test_history_append_record_backfills_empty_title(monkeypatch) -> None:
    """cron 失败补写携带 title：空标题回填，已有标题不覆盖。"""
    receipt: Future[None] = Future()
    receipt.set_result(None)
    monkeypatch.setattr(
        agent_ws_server_module, "append_history_record", lambda **kwargs: None
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *_args, **_kwargs: receipt,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda response, *, response_id: {
            "response_id": response_id,
            "ok": response.ok,
            "payload": response.payload,
        },
    )

    async def _capture_send(_ws, wire):
        pass

    monkeypatch.setattr(agent_ws_server_module, "send_wire_payload", _capture_send)
    # get_session_metadata 走 agent_ws_server 模块级绑定；
    # update_session_metadata 是 handler 内局部导入，需 patch 源模块属性
    from jiuwenswarm.server.runtime.session import session_metadata

    stored = {"title": ""}
    updates: list[dict] = []
    monkeypatch.setattr(
        agent_ws_server_module, "get_session_metadata", lambda _sid, **_kw: dict(stored)
    )
    monkeypatch.setattr(
        session_metadata, "update_session_metadata", lambda **kw: updates.append(kw)
    )
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )

    def _request(req_id: str) -> AgentRequest:
        return AgentRequest(
            request_id=req_id,
            channel_id="__cron__",
            session_id="cron-session",
            req_method=ReqMethod.HISTORY_APPEND_RECORD,
            timestamp=123.5,
            params={
                "request_id": "cron-request-1",
                "channel_id": "web",
                "role": "assistant",
                "content": "[cron] task failed",
                "title": "每日站会纪要",
            },
        )

    await server._handle_history_append_record(
        _FakeWebSocket(), _request("rpc-title"), asyncio.Lock()
    )
    assert updates == [
        {
            "session_id": "cron-session",
            "title": "每日站会纪要",
            "touch_last_message_at": False,
        }
    ]

    stored["title"] = "已有标题"
    await server._handle_history_append_record(
        _FakeWebSocket(), _request("rpc-title-keep"), asyncio.Lock()
    )
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_history_append_record_rejects_missing_session_or_content(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda response, *, response_id: {
            "response_id": response_id,
            "ok": response.ok,
            "payload": response.payload,
        },
    )

    async def _capture_send(_ws, wire):
        sent.append(wire)

    monkeypatch.setattr(agent_ws_server_module, "send_wire_payload", _capture_send)
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    request = AgentRequest(
        request_id="rpc-missing",
        req_method=ReqMethod.HISTORY_APPEND_RECORD,
        params={},
    )

    await server._handle_history_append_record(_FakeWebSocket(), request, asyncio.Lock())

    assert sent == [
        {
            "response_id": "rpc-missing",
            "ok": False,
            "payload": {
                "error": "session_id and content required",
                "code": "BAD_REQUEST",
            },
        }
    ]


@pytest.mark.asyncio
async def test_history_append_record_rejects_invalid_session_id(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        lambda response, *, response_id: {
            "response_id": response_id,
            "ok": response.ok,
            "payload": response.payload,
        },
    )

    async def _capture_send(_ws, wire):
        sent.append(wire)

    monkeypatch.setattr(agent_ws_server_module, "send_wire_payload", _capture_send)
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    request = AgentRequest(
        request_id="rpc-invalid-session",
        req_method=ReqMethod.HISTORY_APPEND_RECORD,
        params={"session_id": "../outside", "content": "must not be written"},
    )

    await server._handle_history_append_record(_FakeWebSocket(), request, asyncio.Lock())

    assert sent == [
        {
            "response_id": "rpc-invalid-session",
            "ok": False,
            "payload": {
                "error": "session_id and content required",
                "code": "BAD_REQUEST",
            },
        }
    ]
