"""旧接入和版本接入消费同一次提交，不能互相改变输出策略。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openjiuwen.harness.tools.todo_store import decode_snapshot

from jiuwenswarm.common.todo_output import (
    TODO_SNAPSHOT_MODE,
    resolve_todo_snapshot_mode,
)
from jiuwenswarm.common.todo_snapshot import project_todo_snapshot


def snapshot():
    return decode_snapshot({"plan_id": "P", "revision": 12, "todos": [
        {"id": "old", "status": "completed", "generation_token": "old"},
        {"id": "A", "status": "in_progress", "generation_token": "current"},
        {"id": "B", "status": "completed", "generation_token": "current"},
    ]})


def test_unconfigured_consumer_keeps_legacy_shape_and_display():
    token = TODO_SNAPSHOT_MODE.set("legacy")
    try:
        result = project_todo_snapshot(snapshot(), "current")
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert [row["id"] for row in result] == ["A", "B"]
    assert [row["status"] for row in result] == ["in_progress", "pending"]
    assert result.todo_meta is None
    assert "plan_id" not in json.dumps(result)
    assert snapshot().todos[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_parallel_consumers_keep_independent_output_and_same_commit():
    committed = snapshot()

    async def consume(mode):
        token = TODO_SNAPSHOT_MODE.set(mode)
        try:
            await asyncio.sleep(0)
            return project_todo_snapshot(committed, "current")
        finally:
            TODO_SNAPSHOT_MODE.reset(token)

    legacy, versioned = await asyncio.gather(consume("legacy"), consume("versioned"))
    assert len(legacy) == 2 and legacy.todo_meta is None
    assert len(versioned) == 3 and versioned[-1]["status"] == "completed"
    assert versioned.todo_meta == {"plan_id": "P", "revision": 12}


def test_only_explicit_host_application_configuration_enables_versions():
    config = {"todo": {"snapshot_modes": {"adapted-app": "versioned"}}}
    assert resolve_todo_snapshot_mode(config, None) == "legacy"
    assert resolve_todo_snapshot_mode(config, "chat-service") == "legacy"
    assert resolve_todo_snapshot_mode(config, "adapted-app") == "versioned"
    assert resolve_todo_snapshot_mode({}, "adapted-app") == "legacy"
    config["todo"]["snapshot_modes"]["adapted-app"] = "typo"
    with pytest.raises(ValueError):
        resolve_todo_snapshot_mode(config, "adapted-app")


@pytest.mark.asyncio
@pytest.mark.parametrize("app_id,expected", [(None, "legacy"), ("adapted-app", "versioned")])
async def test_http_dispatch_uses_host_identity_and_freezes_mode(monkeypatch, app_id, expected):
    from jiuwenswarm.extensions.identity_provider import IdentityStore
    from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
    from jiuwenswarm.server.agent_http_server import AgentHTTPServer

    config = {"todo": {"snapshot_modes": {"adapted-app": "versioned"}}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    seen = []

    async def dispatch(_ctx, _request):
        seen.append(TODO_SNAPSHOT_MODE.get())
        config["todo"]["snapshot_modes"]["adapted-app"] = "legacy"
        await asyncio.sleep(0)
        seen.append(TODO_SNAPSHOT_MODE.get())

    monkeypatch.setattr("jiuwenswarm.server.pipeline.dispatch_parsed_request", dispatch)
    host = SimpleNamespace(_make_ctx=lambda sink, req: SimpleNamespace(sink=sink, request=req))
    request = SimpleNamespace(params={"todo_snapshot_mode": "versioned", "app_id": "adapted-app"})
    identity_token = IdentityStore.set_identity(IdentityInfo(app_id=app_id) if app_id else None)
    before = TODO_SNAPSHOT_MODE.get()
    try:
        await AgentHTTPServer._dispatch_request(host, None, request)
    finally:
        IdentityStore.clear(identity_token)
    assert seen == [expected, expected]
    assert TODO_SNAPSHOT_MODE.get() == before


@pytest.mark.asyncio
async def test_two_ws_consumers_keep_their_connection_mode_when_config_changes(monkeypatch):
    from jiuwenswarm.extensions.identity_provider import IdentityStore
    from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    config = {"todo": {"snapshot_modes": {"adapted-app": "versioned"}}}
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.get_config", lambda: config)

    async def noop(*_args):
        return None

    # The transport receives an identity supplied by the existing host provider.
    monkeypatch.setattr(IdentityStore, "fetch_and_store", noop)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._trigger_agent_server_started_hook = noop
    server._server = None
    server._session_stream_tasks = {}
    server._acp_client_capabilities_by_ws = {}
    seen = {"chat": [], "adapted": []}
    both_started = asyncio.Event()

    class Connection:
        remote_address = ("127.0.0.1", 1)

        def __init__(self, name):
            self.name, self.index = name, 0
            self.finished = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.index += 1
            if self.index == 2:
                await both_started.wait()
                config["todo"]["snapshot_modes"]["adapted-app"] = "legacy"
            if self.index > 2:
                await self.finished.wait()
                raise StopAsyncIteration
            return str(self.index)

        async def send(self, _payload):
            return None

    async def handle(ws, raw, _lock):
        seen[ws.name].append(TODO_SNAPSHOT_MODE.get())
        if all(seen.values()):
            both_started.set()
        if raw == "2":
            ws.finished.set()

    server._handle_message = handle

    async def connect(name, app_id):
        token = IdentityStore.set_identity(IdentityInfo(app_id=app_id))
        before = TODO_SNAPSHOT_MODE.get()
        try:
            await server._connection_handler(Connection(name))
        finally:
            IdentityStore.clear(token)
        assert TODO_SNAPSHOT_MODE.get() == before

    await asyncio.wait_for(asyncio.gather(connect("chat", "chat-service"),
                                         connect("adapted", "adapted-app")), timeout=5)
    assert seen == {"chat": ["legacy", "legacy"], "adapted": ["versioned", "versioned"]}
