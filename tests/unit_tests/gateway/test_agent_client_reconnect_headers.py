"""Automatic reconnect must retain the last explicit connection's parameters."""
import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from gateway.routing.agent_client import WebSocketAgentServerClient


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.frames.put_nowait(json.dumps({
            'type': 'event', 'event': 'connection.ack', 'payload': {'status': 'ready'},
        }))
        self.sent = []

    async def recv(self):
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        self.frames.put_nowait(json.dumps(encode_agent_response_for_wire(
            AgentResponse(request['request_id'], request['channel'], payload={'received': True}),
            response_id=request['request_id'],
        )))

    async def close(self):
        pass


def transport(monkeypatch):
    calls, sockets = [], []

    async def connect(uri, **kwargs):
        calls.append((uri, kwargs))
        socket = Socket()
        sockets.append(socket)
        return socket

    monkeypatch.setattr('websockets.legacy.client.connect', connect)
    return calls, sockets


async def drop(client, socket):
    socket.frames.put_nowait(ConnectionClosedError(None, None))
    await asyncio.wait_for(client._receiver_task, 1)


async def test_reconnect_reuses_headers_without_replaying_business_request(monkeypatch):
    calls, sockets = transport(monkeypatch)
    client = WebSocketAgentServerClient()
    headers = {'Authorization': 'Bearer original', 'X-Session-Context': 'user-context'}
    try:
        await client.connect('wss://ingress/agent/ws?target=binding', extra_headers=headers)
        headers['Authorization'] = 'Bearer caller-mutated'
        await drop(client, sockets[0])
        response = await client.send_request(E2AEnvelope(
            request_id='new-request', channel='web', method='session.list', params={},
        ), timeout=1)
        assert response.ok
        assert calls[0] == calls[1]
        assert dict(calls[1][1]['extra_headers'])['Authorization'] == 'Bearer original'
        assert sum(len(socket.sent) for socket in sockets) == 1
    finally:
        await client.disconnect()


@pytest.mark.parametrize('replacement', [None, {'Authorization': 'Bearer rotated'}])
async def test_explicit_new_connection_replaces_saved_reconnect_headers(monkeypatch, replacement):
    calls, sockets = transport(monkeypatch)
    client = WebSocketAgentServerClient()
    try:
        await client.connect('wss://old-host/ws', extra_headers={'Authorization': 'Bearer old'})
        await client.connect('wss://new-host/ws', extra_headers=replacement)
        await drop(client, sockets[-1])
        await client.send_request(E2AEnvelope(
            request_id='new-host-request', channel='web', method='session.list', params={},
        ), timeout=1)
        assert calls[-1] == calls[-2]
        assert calls[-1][0] == 'wss://new-host/ws'
        assert dict(calls[-1][1].get('extra_headers', [])) == (replacement or {})
    finally:
        await client.disconnect()


async def test_real_authenticated_server_accepts_automatic_reconnect(monkeypatch):
    from http import HTTPStatus
    from websockets.exceptions import ConnectionClosed
    from websockets.legacy.server import serve

    headers, requests = [], []

    async def authenticate(path, received):
        headers.append(received.get('Authorization'))
        if received.get('Authorization') != 'Bearer authenticated':
            return HTTPStatus.UNAUTHORIZED, [], b'unauthorized'
        return None

    async def server(ws, path):
        await ws.send(json.dumps({
            'type': 'event', 'event': 'connection.ack', 'payload': {'status': 'ready'},
        }))
        try:
            async for raw in ws:
                request = json.loads(raw)
                requests.append(request['request_id'])
                await ws.send(json.dumps(encode_agent_response_for_wire(
                    AgentResponse(request['request_id'], request['channel'], payload={'received': True}),
                    response_id=request['request_id'],
                )))
                if request['request_id'] == 'first':
                    await ws.close(code=1011, reason='test connection interruption')
        except ConnectionClosed:
            pass

    async with serve(server, '127.0.0.1', 0, process_request=authenticate) as listener:
        port = listener.sockets[0].getsockname()[1]
        client = WebSocketAgentServerClient()
        try:
            await client.connect(f'ws://127.0.0.1:{port}/agent/ws', extra_headers={
                'Authorization': 'Bearer authenticated',
            })
            response = await client.send_request(E2AEnvelope(
                request_id='first', channel='web', method='session.list', params={},
            ), timeout=1)
            assert response.ok
            await asyncio.wait_for(client._receiver_task, 2)
            response = await client.send_request(E2AEnvelope(
                request_id='second', channel='web', method='session.list', params={},
            ), timeout=1)
            assert response.ok
            assert headers == ['Bearer authenticated', 'Bearer authenticated']
            assert requests == ['first', 'second']
        finally:
            await client.disconnect()
