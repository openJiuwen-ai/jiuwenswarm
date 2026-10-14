# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The mailbox follows the Gateway connection Front hands to the Runtime.

In the Front/Worker deployment the Gateway socket reaches the Runtime through
``attach_gateway_connection`` / ``on_gateway_disconnect``, not through
``_connection_handler``. The mailbox delivers queued cross-Session messages
only while it is available, so these hooks must drive its availability.
"""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult,
    SessionMessageService,
    SessionMessageSource,
)
from jiuwenswarm.server.runtime.session.session_message_store import (
    SessionMessageStore,
)


class _Admission:
    def __init__(self) -> None:
        self.active: set[str] = set()

    async def begin_session_message(self, session_id: str, run_id: str) -> None:
        self.active.add(session_id)

    async def end_session_message(self, session_id: str, run_id: str) -> None:
        self.active.discard(session_id)

    def is_user_active(self, session_id: str) -> bool:
        return False

    def is_session_message_active(self, session_id: str) -> bool:
        return session_id in self.active


def _metadata(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "title": session_id,
        "user_id": "user-1",
        "channel_id": "web",
        "mode": "agent.work.normal",
        "project_id": "project-1",
    }


def _mailbox(tmp_path, delivered: list[str]) -> SessionMessageService:
    async def execute(record):
        delivered.append(record.content)
        return SessionMessageExecutionResult(status="succeeded")

    # Like _install_session_message_service: no Gateway yet at construction.
    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_Admission(),
        execute=execute,
        available=False,
    )
    service._session_metadata = _metadata
    return service


def _server(service: SessionMessageService) -> SimpleNamespace:
    execution = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(),
        cancel_all_team_stream_tasks=AsyncMock(),
    )
    server = SimpleNamespace(
        _current_ws=None,
        _current_send_lock=None,
        _session_message_service=service,
        _session_stream_tasks={},
        _heartbeat_runtime=SimpleNamespace(
            execution=SimpleNamespace(active_session_ids=lambda: ())
        ),
        _clear_ws_acp_client_capabilities=lambda ws: None,
        _execution_runtime=lambda: execution,
        _stop_scheduler=AsyncMock(),
    )
    for name in (
        "_sync_session_messaging_availability",
        "_schedule_session_messaging_availability",
    ):
        setattr(server, name, MethodType(getattr(AgentWebSocketServer, name), server))
    return server


async def _send(service: SessionMessageService, call: str) -> None:
    source = SessionMessageSource(
        session_id="doc-a",
        request_id="request-1",
        tool_call_id=call,
        idempotency_key=call,
        user_id="user-1",
    )
    await service.send_message(
        source, input_mode="follow_up", target_session_id="doc-b", message=call
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_attached_gateway_delivers_queued_messages(tmp_path) -> None:
    delivered: list[str] = []
    service = _mailbox(tmp_path, delivered)
    server = _server(service)

    AgentWebSocketServer.attach_gateway_connection(server, object(), asyncio.Lock())
    await _send(service, "first")
    await _settle()

    assert delivered == ["first"]
    await service.stop()


@pytest.mark.asyncio
async def test_detached_gateway_holds_messages(tmp_path) -> None:
    delivered: list[str] = []
    service = _mailbox(tmp_path, delivered)
    server = _server(service)
    ws = object()

    AgentWebSocketServer.attach_gateway_connection(server, ws, asyncio.Lock())
    await _settle()
    await AgentWebSocketServer.on_gateway_disconnect(server, object(), "stale")
    await _send(service, "while attached")
    await _settle()
    await AgentWebSocketServer.on_gateway_disconnect(server, ws, "gateway")
    await _send(service, "while detached")
    await _settle()

    assert delivered == ["while attached"]
    await service.stop()


@pytest.mark.asyncio
async def test_detach_before_the_attach_sync_runs_leaves_it_unavailable(
    tmp_path,
) -> None:
    delivered: list[str] = []
    service = _mailbox(tmp_path, delivered)
    server = _server(service)
    ws = object()

    AgentWebSocketServer.attach_gateway_connection(server, ws, asyncio.Lock())
    await AgentWebSocketServer.on_gateway_disconnect(server, ws, "gateway")
    await _send(service, "after detach")
    await _settle()

    assert delivered == []
    await service.stop()
