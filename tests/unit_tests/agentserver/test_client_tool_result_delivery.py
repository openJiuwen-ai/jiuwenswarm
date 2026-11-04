# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A client tool result reaches the agent as a follow-up input, after what the Session is busy with."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.runtime.client_tool_adapter import ClientToolCall, ClientToolCallStatus, ClientToolDeliveryError
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.server import agent_ws_server as server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter.interface import is_external_user_authored_dispatch

CALL = ClientToolCall(
    call_id="call-1", session_id="s1", tool_name="client__read_page", client_tool="read_page",
    arguments={}, fingerprint="f", owner_client_id="pane-1", status=ClientToolCallStatus.COMPLETED,
    created_at=0.0, deadline=1.0,
)


class _Runtime:
    def __init__(self) -> None:
        self.requests = []
        self.start = AsyncMock()
        self.create_or_resume_session = AsyncMock()

    def stream(self, request, **_kwargs):
        self.requests.append(request)

        async def events():
            for payload in (
                {"event_type": "runtime.accepted", "input_delivery": "chat"},
                {"event_type": "chat.delta", "content": "page one says hi"},
                {"event_type": "chat.final", "content": "done"},
            ):
                yield RuntimeEvent(request_id=request.request_id, channel_id="web", session_id="s1", payload=payload)

        return events()


def _server(runtime: _Runtime, pending: list[bool]) -> AgentWebSocketServer:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._current_ws = None
    server._heartbeat_runtime = SimpleNamespace(
        admission=SimpleNamespace(has_pending_interaction=lambda session_id: pending.pop(0) if pending else False),
    )
    server._execution_runtime = lambda: runtime
    server.send_push = AsyncMock()
    return server


def _patched():
    return (
        patch.object(server_module, "get_session_metadata", return_value={"mode": "agent", "channel_id": "web"}),
        patch.object(server_module, "build_server_push_message",
                     side_effect=lambda **kwargs: kwargs),
        patch.object(server_module, "_CLIENT_TOOL_DELIVERY_POLL_SECONDS", 0),
    )


async def test_the_result_goes_in_as_a_follow_up_and_its_turn_reaches_the_clients() -> None:
    runtime = _Runtime()
    server = _server(runtime, [])
    metadata, push, poll = _patched()

    with metadata, push, poll:
        await server._deliver_client_tool_result(CALL, "<client_tool_result ...>")

    [request] = runtime.requests
    assert request.params["input_mode"] == "follow_up"
    assert request.params["query"] == "<client_tool_result ...>"
    assert request.params["client_tool_result"] == {"call_id": "call-1", "tool_name": "client__read_page"}
    # The client's output must never pass for something the user wrote.
    assert not is_external_user_authored_dispatch(request.params, channel_id="web", request_method=request.req_method)
    pushed = [call.args[0]["payload"] for call in server.send_push.await_args_list]
    assert [payload["event_type"] for payload in pushed] == ["chat.delta", "chat.final"]
    assert all(payload["client_tool_result"]["call_id"] == "call-1" for payload in pushed)


async def test_the_result_waits_while_the_session_waits_on_the_user() -> None:
    runtime = _Runtime()
    server = _server(runtime, [True, True, False])
    metadata, push, poll = _patched()

    with metadata, push, poll:
        await asyncio.wait_for(server._deliver_client_tool_result(CALL, "<client_tool_result ...>"), 5)

    assert len(runtime.requests) == 1


async def test_a_session_that_takes_no_client_tools_fails_the_delivery() -> None:
    runtime = _Runtime()
    server = _server(runtime, [])
    _, push, poll = _patched()

    with patch.object(server_module, "get_session_metadata", return_value={"mode": "team"}), push, poll:
        with pytest.raises(ClientToolDeliveryError):
            await server._deliver_client_tool_result(CALL, "<client_tool_result ...>")

    assert runtime.requests == []


async def test_a_question_left_unanswered_too_long_fails_the_delivery() -> None:
    runtime = _Runtime()
    server = _server(runtime, [True] * 1000)
    server._client_tool_delivery_wait_s = 0
    metadata, push, poll = _patched()

    with metadata, push, poll:
        with pytest.raises(ClientToolDeliveryError):
            await asyncio.wait_for(server._deliver_client_tool_result(CALL, "<client_tool_result ...>"), 5)

    assert runtime.requests == []


def test_a_bad_client_tools_setting_does_not_stop_the_server(monkeypatch) -> None:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = SimpleNamespace(set_client_tool_adapter=lambda adapter: setattr(server, "_attached", adapter))
    monkeypatch.setattr(server_module, "get_config", lambda: {"client_tools": {
        "default_timeout_s": "two minutes", "disconnect_grace_s": -5, "delivery_wait_s": None,
    }})

    server._install_client_tool_adapter()

    assert server._attached is server._client_tool_adapter
    assert server._client_tool_adapter._default_timeout_s == 120.0
    assert server._client_tool_adapter._disconnect_grace_s == 30.0
    assert server._client_tool_delivery_wait_s == 600.0
