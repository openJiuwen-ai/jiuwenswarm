"""A transport subscriber must not own another connection's execution lifetime."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.transports import push_registry


class FakeWebSocket:
    def __init__(self, port):
        self.remote_address = ("127.0.0.1", port)
        self.incoming = asyncio.Queue()
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        payload = await self.incoming.get()
        if payload is None:
            raise StopAsyncIteration
        return payload


def make_server(monkeypatch):
    from jiuwenswarm.extensions.identity_provider import IdentityStore
    import jiuwenswarm.agents.harness.team as team

    monkeypatch.setattr(push_registry, "_REGISTRY", push_registry.PushRegistry())
    monkeypatch.setattr(IdentityStore, "fetch_and_store", AsyncMock(return_value=None))
    cancel_teams = AsyncMock()
    monkeypatch.setattr(team, "cancel_all_team_stream_tasks_across_managers", cancel_teams)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._trigger_agent_server_started_hook = AsyncMock()
    server._stop_scheduler = AsyncMock()
    server._agent_manager = SimpleNamespace(cancel_all_inflight_work=AsyncMock())
    server._session_stream_tasks = {}
    server._server = None
    server._clear_ws_acp_client_capabilities = Mock()
    return server, cancel_teams


@pytest.mark.parametrize("disconnect_has_request", [False, True])
def test_disconnect_cancels_only_owned_consumers_and_preserves_shared_session(monkeypatch, disconnect_has_request):
    async def exercise():
        server, cancel_teams = make_server(monkeypatch)
        first, second = FakeWebSocket(1), FakeWebSocket(2)
        started = {first: asyncio.Event(), second: asyncio.Event()}
        stopped = {first: asyncio.Event(), second: asyncio.Event()}

        async def handle(ws, raw, lock):
            task = asyncio.current_task()
            server._session_stream_tasks.setdefault("shared-session", {})[task] = asyncio.Event()
            started[ws].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped[ws].set()

        server._handle_message = handle
        connections = [asyncio.create_task(server._connection_handler(ws)) for ws in (first, second)]
        try:
            second.incoming.put_nowait("request")
            await asyncio.wait_for(started[second].wait(), 1)
            if disconnect_has_request:
                first.incoming.put_nowait("request")
                await asyncio.wait_for(started[first].wait(), 1)
            first.incoming.put_nowait(None)
            await asyncio.wait_for(connections[0], 1)
            assert stopped[first].is_set() == disconnect_has_request
            assert not stopped[second].is_set()
            assert len(server._session_stream_tasks["shared-session"]) == 1
            assert push_registry.get_push_registry().subscriber_count() == 1
            server._agent_manager.cancel_all_inflight_work.assert_not_awaited()
            server._stop_scheduler.assert_not_awaited()
            cancel_teams.assert_not_awaited()
        finally:
            first.incoming.put_nowait(None)
            second.incoming.put_nowait(None)
            await asyncio.wait_for(asyncio.gather(*connections), 1)
        assert stopped[second].is_set()
        assert server._session_stream_tasks == {}

    asyncio.run(exercise())


def test_global_shutdown_still_cancels_all_work_and_stops_scheduler(monkeypatch):
    async def exercise():
        import jiuwenswarm.server.agent_ws_server as ws_module
        from jiuwenswarm.server.runtime.session import kv_cache_product_hooks

        server, cancel_teams = make_server(monkeypatch)
        listener = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        server._server = listener
        server._loop_lag_task = None
        server._restore_runtime_push_host = Mock()
        server._stop_personal_context_best_effort = AsyncMock()
        server._jiuwenbox_runner = SimpleNamespace(stop=AsyncMock())
        server._session_stream_tasks = {"leftover": {object(): asyncio.Event()}}
        monkeypatch.setattr(ws_module, "_startup_warmup_task", None)
        monkeypatch.setattr(ws_module, "is_enterprise", lambda: True)
        monkeypatch.setattr(kv_cache_product_hooks, "cancel_pending_tasks", AsyncMock())
        await server.stop()
        listener.close.assert_called_once()
        listener.wait_closed.assert_awaited_once()
        server._agent_manager.cancel_all_inflight_work.assert_awaited_once()
        server._stop_scheduler.assert_awaited_once()
        cancel_teams.assert_awaited_once()
        assert server._session_stream_tasks == {}

    asyncio.run(exercise())
