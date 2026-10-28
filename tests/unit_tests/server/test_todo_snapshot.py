from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from openjiuwen.harness.schema.task import TodoStatus
from openjiuwen.harness.tools.todo_store import decode_snapshot

from jiuwenswarm.common.todo_snapshot import (
    TODO_SNAPSHOT_MODE,
    project_todo_snapshot,
    resolve_todo_snapshot_mode,
    format_todos_for_frontend,
    load_todo_snapshot_for_frontend,
)


def test_format_todos_keeps_completed_and_drops_cancelled():
    items = [
        SimpleNamespace(id="a", content="pending", activeForm="doing", status=TodoStatus.PENDING),
        SimpleNamespace(id="b", content="done", activeForm="done", status=TodoStatus.COMPLETED),
        SimpleNamespace(id="c", content="cancel", activeForm="cancel", status=TodoStatus.CANCELLED),
    ]

    assert [item["id"] for item in format_todos_for_frontend(items)] == ["a", "b"]


def test_format_todos_defaults_malformed_status_to_pending():
    items = [
        {"id": "dict", "content": "dict status", "status": {}},
        {"id": "list", "content": "list status", "status": []},
        {"id": "none", "content": "none status", "status": None},
    ]

    assert [item["status"] for item in format_todos_for_frontend(items)] == [
        "pending",
        "pending",
        "pending",
    ]


def test_load_snapshot_accepts_json_and_rejects_unsafe_session(tmp_path, monkeypatch):
    root = tmp_path / "todo"
    snapshot = root / "web_session_1" / "todo.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps([{"id": "t1", "content": "restore", "status": "in_progress"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir", lambda: root
    )

    assert load_todo_snapshot_for_frontend("web_session_1") == [{
        "id": "t1",
        "content": "restore",
        "activeForm": "restore",
        "status": "in_progress",
    }]
    assert load_todo_snapshot_for_frontend("../web_session_1") is None


def test_legacy_snapshot_keeps_generation_filter_and_serial_display(tmp_path):
    root = tmp_path / "tenant" / "agent" / "jiuwenclaw_workspace" / "todo"
    snapshot = root / "web_session_2" / "todo.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps([
            {
                "id": "old",
                "content": "old generation",
                "status": "completed",
                "generation_token": "old-token",
            },
            {
                "id": "current-open",
                "content": "current open",
                "status": "in_progress",
                "generation_token": "current-token",
            },
            {
                "id": "current-late",
                "content": "completed out of order",
                "status": "completed",
                "generation_token": "current-token",
            },
        ]),
        encoding="utf-8",
    )

    restored = load_todo_snapshot_for_frontend(
        "web_session_2",
        todo_root=root,
        generation_token="current-token",
    )

    assert [item["id"] for item in restored] == ["current-open", "current-late"]
    assert [item["status"] for item in restored] == ["in_progress", "pending"]
    assert restored.todo_meta is None


@pytest.mark.parametrize("mode", ["legacy", "versioned"])
def test_empty_and_read_failure_are_distinct(tmp_path, mode):
    path = tmp_path / "s" / "todo.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"plan_id": "new", "revision": 7, "todos": []}))
    token = TODO_SNAPSHOT_MODE.set(mode)
    try:
        snapshot = load_todo_snapshot_for_frontend("s", todo_root=tmp_path)
    finally:
        TODO_SNAPSHOT_MODE.reset(token)
    assert snapshot == []
    assert snapshot.todo_meta == ({"plan_id": "new", "revision": 7} if mode == "versioned" else None)
    path.write_text("broken")
    assert load_todo_snapshot_for_frontend("s", todo_root=tmp_path) is None
    path.write_text("42")
    assert load_todo_snapshot_for_frontend("s", todo_root=tmp_path) is None


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
