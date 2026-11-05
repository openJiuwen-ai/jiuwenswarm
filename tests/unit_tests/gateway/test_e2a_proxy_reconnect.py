"""Requests through the proxy must recover a dropped AgentServer transport."""

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedError

from openjiuwen_protocol.e2a.agent_models import AgentResponse
from openjiuwen_protocol.e2a.wire_codec import encode_agent_response_for_wire
from openjiuwen_protocol.message import ReqMethod
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from agent_gateway.routing.e2a_proxy import fetch_agent_unary, proxy_unary_request


class Channel:
    channel_id = "web"

    def __init__(self):
        self.responses = []

    async def send_response(self, ws, req_id, **response):
        self.responses.append(response)


async def invoke(client, entry, request_id="create", timeout=1):
    if entry == "fetch":
        return await fetch_agent_unary(
            agent_client=client, req_method=ReqMethod.SESSION_CREATE,
            params={}, session_id=None, user_id=None, channel_id="web",
            timeout_seconds=timeout,
        )
    channel = Channel()
    await proxy_unary_request(
        channel=channel, agent_client=client, ws=object(), req_id=request_id,
        params={}, session_id=None, user_id=None,
        req_method=ReqMethod.SESSION_CREATE, timeout_seconds=timeout,
    )
    response = channel.responses[-1]
    return response["ok"], response.get("payload") or response


class Socket:
    def __init__(self, ack_gate=None):
        self.frames = asyncio.Queue()
        self.frames.put_nowait(json.dumps({
            "type": "event", "event": "connection.ack",
            "payload": {"status": "ready", "readiness": "AGENT_READY"},
        }))
        self.ack_gate = ack_gate
        self.reading_ack = asyncio.Event()
        self.sent = []
        self.closed = False

    async def recv(self):
        if not self.reading_ack.is_set():
            self.reading_ack.set()
            if self.ack_gate is not None:
                await self.ack_gate.wait()
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        self.frames.put_nowait(json.dumps(encode_agent_response_for_wire(
            AgentResponse(request["request_id"], request["channel"], payload={"created": True}),
            response_id=request["request_id"],
        )))

    async def close(self):
        self.closed = True


class Transport:
    def __init__(self):
        self.calls = []
        self.sockets = []
        self.error = None
        self.ack_gate = None

    async def connect(self, uri, **kwargs):
        self.calls.append((uri, kwargs))
        if self.error:
            raise self.error
        socket = Socket(self.ack_gate)
        self.sockets.append(socket)
        return socket


@pytest.fixture
def transport(monkeypatch):
    value = Transport()
    monkeypatch.setattr("websockets.legacy.client.connect", value.connect)
    return value


async def drop(client, socket):
    socket.frames.put_nowait(ConnectionClosedError(None, None))
    await asyncio.wait_for(client._receiver_task, 1)
    assert not client.server_ready


@pytest.mark.parametrize("entry", ["proxy", "fetch"])
async def test_proxy_recovers_after_disconnect_without_replaying_requests(transport, entry):
    client = WebSocketAgentServerClient()
    try:
        await client.connect("ws://agentserver:18092", extra_headers={"Authorization": "Bearer test"})
        assert (await invoke(client, entry))[0]
        await drop(client, transport.sockets[0])
        assert (await invoke(client, entry))[0]
        assert transport.calls[0] == transport.calls[1]
        assert [len(ws.sent) for ws in transport.sockets] == [1, 1]
        assert client.server_ready
    finally:
        await client.disconnect()


@pytest.mark.parametrize("entry", ["proxy", "fetch"])
async def test_real_server_restart_recovers_through_proxy(entry):
    from http import HTTPStatus
    from websockets.legacy.server import serve

    headers, requests = [], []

    async def authenticate(path, received):
        headers.append(received.get("Authorization"))
        if received.get("Authorization") != "Bearer test":
            return HTTPStatus.UNAUTHORIZED, [], b"unauthorized"
        return None

    async def server(ws, path):
        await ws.send(json.dumps({
            "type": "event", "event": "connection.ack",
            "payload": {"status": "ready", "readiness": "AGENT_READY"},
        }))
        async for raw in ws:
            request = json.loads(raw)
            requests.append(request)
            await ws.send(json.dumps(encode_agent_response_for_wire(
                AgentResponse(request["request_id"], request["channel"], payload={"created": True}),
                response_id=request["request_id"],
            )))

    client = WebSocketAgentServerClient()
    try:
        async with serve(server, "127.0.0.1", 0, process_request=authenticate) as listener:
            port = listener.sockets[0].getsockname()[1]
            await client.connect(f"ws://127.0.0.1:{port}/agent/ws", extra_headers={
                "Authorization": "Bearer test",
            })
            assert (await invoke(client, entry, "before-restart"))[0]
            receiver = client._receiver_task
        await asyncio.wait_for(receiver, 2)
        assert not client.server_ready
        # Rebind the same endpoint, preserving the Gateway client instance.
        async with serve(server, "127.0.0.1", port, process_request=authenticate):
            assert (await invoke(client, entry, "after-restart"))[0]
            assert client.server_ready
            assert headers == ["Bearer test", "Bearer test"]
            assert len(requests) == 2
    finally:
        await client.disconnect()


async def test_inflight_request_is_not_replayed_after_disconnect(transport):
    client = WebSocketAgentServerClient()
    try:
        await client.connect("ws://agentserver:18092")
        socket = transport.sockets[0]

        async def send_then_drop(raw):
            socket.sent.append(json.loads(raw))
            socket.frames.put_nowait(ConnectionClosedError(None, None))

        socket.send = send_then_drop
        ok, payload = await invoke(client, "proxy", "interrupted")
        assert not ok and payload["code"] == "SERVICE_UNAVAILABLE"
        assert len(transport.calls) == 1
        assert (await invoke(client, "proxy", "new-request"))[0]
        assert [[request["request_id"] for request in ws.sent] for ws in transport.sockets] == [
            ["interrupted"], ["new-request"],
        ]
    finally:
        await client.disconnect()


@pytest.mark.parametrize("entry", ["proxy", "fetch"])
async def test_proxy_retries_on_next_request_after_connection_refused(transport, entry):
    client = WebSocketAgentServerClient()
    try:
        await client.connect("ws://agentserver:18092")
        await drop(client, transport.sockets[0])
        transport.error = ConnectionRefusedError("backend still restarting")
        ok, payload = await invoke(client, entry)
        assert not ok and payload["code"] == "SERVICE_UNAVAILABLE"
        assert len(transport.calls) == 2
        transport.error = None
        assert (await invoke(client, entry))[0]
        assert len(transport.calls) == 3
        assert sum(len(ws.sent) for ws in transport.sockets) == 1
    finally:
        await client.disconnect()


@pytest.mark.parametrize("entry", ["proxy", "fetch"])
async def test_explicit_disconnect_after_transport_loss_disables_reconnect(transport, entry):
    client = WebSocketAgentServerClient()
    await client.connect("ws://agentserver:18092")
    await drop(client, transport.sockets[0])
    await client.disconnect()
    ok, payload = await invoke(client, entry)
    assert not ok and payload["code"] == "SERVICE_UNAVAILABLE"
    assert len(transport.calls) == 1
    assert not client.can_reconnect


async def test_concurrent_proxy_requests_wait_for_one_reconnect_handshake(transport):
    client = WebSocketAgentServerClient()
    tasks = []
    try:
        await client.connect("ws://agentserver:18092")
        await drop(client, transport.sockets[0])
        gate = transport.ack_gate = asyncio.Event()
        tasks.append(asyncio.create_task(invoke(client, "proxy", "first")))
        async with asyncio.timeout(1):
            while len(transport.sockets) < 2:
                await asyncio.sleep(0)
        socket = transport.sockets[-1]
        await asyncio.wait_for(socket.reading_ack.wait(), 1)
        tasks.extend(asyncio.create_task(invoke(client, "proxy", f"next-{i}")) for i in range(3))
        await asyncio.sleep(0)
        assert not socket.sent
        gate.set()
        results = await asyncio.gather(*tasks)
        assert all(ok for ok, _ in results)
        assert len(transport.calls) == 2
        assert len(socket.sent) == 4
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("entry", ["proxy", "fetch"])
async def test_reconnect_timeout_closes_partial_socket_and_allows_next_request(transport, entry):
    client = WebSocketAgentServerClient()
    try:
        await client.connect("ws://agentserver:18092")
        await drop(client, transport.sockets[0])
        transport.ack_gate = asyncio.Event()
        ok, payload = await invoke(client, entry, timeout=0.05)
        assert not ok and payload["code"] == "AGENT_SERVER_TIMEOUT"
        assert transport.sockets[-1].closed
        assert not transport.sockets[-1].sent
        transport.ack_gate = None
        assert (await invoke(client, entry))[0]
        assert len(transport.calls) == 3
    finally:
        await client.disconnect()
