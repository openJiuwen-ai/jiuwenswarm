"""Exercise actual WS handshakes: Node subscribers must not own reverse RPC."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.transports import push_registry


@pytest.mark.parametrize("modern", [False, True])
def test_node_and_gateway_ws_handshakes_select_only_gateway_owner(monkeypatch, modern):
    asyncio.run(_exercise_ws_handshakes(monkeypatch, modern))


async def _exercise_ws_handshakes(monkeypatch, modern):
    from jiuwenswarm.extensions.identity_provider import IdentityStore
    import jiuwenswarm.agents.harness.team as team

    if modern:
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect
        # Exercise the real Gateway client's modern fallback too.
        monkeypatch.setitem(sys.modules, "websockets.legacy.client", None)
        headers_key = "additional_headers"
    else:
        from websockets.legacy.server import serve
        from websockets.legacy.client import connect
        headers_key = "extra_headers"

    registry = push_registry.PushRegistry()
    monkeypatch.setattr(push_registry, "_REGISTRY", registry)
    monkeypatch.setattr(IdentityStore, "fetch_and_store", AsyncMock(return_value=None))
    monkeypatch.setattr(team, "cancel_all_team_stream_tasks_across_managers", AsyncMock())
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._trigger_agent_server_started_hook = AsyncMock()
    server._stop_scheduler = AsyncMock()
    server._agent_manager = SimpleNamespace(cancel_all_inflight_work=AsyncMock())
    server._session_stream_tasks = {}
    server._clear_ws_acp_client_capabilities = lambda _ws: None

    async def echo(ws, raw, _lock):
        await ws.send(raw)

    server._handle_message = echo
    gateway = WebSocketAgentServerClient()
    async with serve(server._connection_handler, "127.0.0.1", 0) as listener:
        uri = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        # Same headers as relayclaw-connection.ts; do not modify the Node client.
        async with connect(uri, **{headers_key: {"Origin": "http://127.0.0.1"}}) as node:
            assert json.loads(await node.recv())["event"] == "connection.ack"
            assert not registry.reverse_rpc_ready()
            for channel in ("officeclaw", "web"):
                frame = json.dumps({"channel_id": channel, "req_method": "chat.send"})
                await node.send(frame)
                assert await asyncio.wait_for(node.recv(), 1) == frame
            await gateway.connect(uri)
            try:
                assert gateway.server_ready
                assert registry.reverse_rpc_ready()
                owner = registry._reverse_rpc_owner_id
                # A later ordinary subscriber must not replace the Gateway.
                async with connect(uri) as other_node:
                    await other_node.recv()
                    assert registry._reverse_rpc_owner_id == owner
                    assert await registry.push({"request_id": "file", "body": {}}) == 3
                    assert json.loads(await node.recv())["request_id"] == "file"
                    assert json.loads(await other_node.recv())["request_id"] == "file"
            finally:
                await gateway.disconnect()
            async with asyncio.timeout(2):
                while registry.reverse_rpc_ready():
                    await asyncio.sleep(0.01)
            assert registry.subscriber_count() == 1
