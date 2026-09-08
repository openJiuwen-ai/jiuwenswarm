import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def make_request(sid="new", *, acknowledge=True, channel="desktop"):
    return AgentRequest(request_id=f"req-{sid}", channel_id=channel,
                        session_id=sid, req_method=ReqMethod.CHAT_SEND,
                        params={"query": "hello"},
                        metadata={"require_admission_ack": acknowledge})


def make_server():
    server = object.__new__(AgentWebSocketServer)
    server._active_desktop_chat_streams = {f"s{i}": 1 for i in range(5)}
    server._agent_manager = None
    server._handle_stream_impl = AsyncMock()
    return server


def frames(ws):
    return [json.loads(call.args[0]) for call in ws.send.call_args_list]


def test_capacity_is_readonly_and_matches_same_session_admission():
    server = make_server()
    assert server._desktop_chat_capacity("new") == {"allowed": False, "activeSessions": 5, "limit": 5}
    assert server._desktop_chat_capacity("s0")["allowed"] is True
    assert server._active_desktop_chat_streams == {f"s{i}": 1 for i in range(5)}
    server._end_desktop_chat_stream(make_request("s0"))
    assert server._desktop_chat_capacity("new")["allowed"] is True


@pytest.mark.asyncio
async def test_rejected_stream_is_structured_failure_and_never_executes():
    server = make_server()
    ws = SimpleNamespace(send=AsyncMock())
    await server._handle_stream(ws, make_request(), asyncio.Lock())
    wire = frames(ws)[0]
    assert wire["status"] == "failed"
    assert wire["body"]["details"]["code"] == "DESKTOP_SESSION_LIMIT"
    assert wire["body"]["details"]["activeSessions"] == 5
    assert "new" not in server._active_desktop_chat_streams
    server._handle_stream_impl.assert_not_called()


@pytest.mark.asyncio
async def test_ack_precedes_execution_and_slot_released_on_failure():
    server = make_server()
    server._active_desktop_chat_streams = {}
    ws = SimpleNamespace(send=AsyncMock())

    async def execute(*args):
        assert frames(ws)[0]["body"]["event_type"] == "chat.accepted"
        assert server._active_desktop_chat_streams == {"new": 1}
        raise RuntimeError("execution failed")

    server._handle_stream_impl = execute
    with pytest.raises(RuntimeError, match="execution failed"):
        await server._handle_stream(ws, make_request(), asyncio.Lock())
    assert server._active_desktop_chat_streams == {}


@pytest.mark.asyncio
async def test_ack_send_failure_releases_slot_without_execution():
    server = make_server()
    server._active_desktop_chat_streams = {}
    ws = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("disconnected")))
    with pytest.raises(RuntimeError, match="disconnected"):
        await server._handle_stream(ws, make_request(), asyncio.Lock())
    assert server._active_desktop_chat_streams == {}
    server._handle_stream_impl.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_client_does_not_receive_extra_ack():
    server = make_server()
    server._active_desktop_chat_streams = {}
    ws = SimpleNamespace(send=AsyncMock())
    await server._handle_stream(ws, make_request(acknowledge=False), asyncio.Lock())
    assert frames(ws) == []
    server._handle_stream_impl.assert_awaited_once()



@pytest.mark.asyncio
async def test_capacity_rpc_uses_real_wire_without_agent_or_history():
    server = make_server()
    ws = SimpleNamespace(send=AsyncMock())
    await server._handle_message(ws, json.dumps({
        "protocol_version": "1.0", "request_id": "capacity-rpc", "channel": "desktop",
        "method": "chat.capacity", "params": {}, "is_stream": False,
    }), asyncio.Lock())
    wire = frames(ws)[0]
    assert wire["status"] == "succeeded"
    assert wire["body"]["result"] == {"allowed": False, "activeSessions": 5, "limit": 5}
    server._handle_stream_impl.assert_not_called()


@pytest.mark.asyncio
async def test_competing_sessions_only_one_claims_last_slot_and_cancel_releases():
    server = make_server()
    server._active_desktop_chat_streams.pop("s4")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(*args):
        entered.set()
        await release.wait()

    server._handle_stream_impl = execute
    first_ws = SimpleNamespace(send=AsyncMock())
    first = asyncio.create_task(server._handle_stream(first_ws, make_request("first"), asyncio.Lock()))
    await entered.wait()
    second_ws = SimpleNamespace(send=AsyncMock())
    await server._handle_stream(second_ws, make_request("second"), asyncio.Lock())
    assert frames(second_ws)[0]["status"] == "failed"
    assert server._desktop_chat_capacity("third")["activeSessions"] == 5
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert server._desktop_chat_capacity("third")["activeSessions"] == 4
