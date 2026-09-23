"""Task ownership across Gateway and AgentServer transport/data boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.video_duplex.backend import task_adapter
from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks import bridge
from jiuwenswarm.extensions.video_duplex.backend.tasks.checkpoint import TaskCheckpoint
from jiuwenswarm.extensions.video_duplex.backend.tasks.execution import bind_task_execution
from jiuwenswarm.extensions.video_duplex.backend.tasks.server_adapter import VoiceTaskServerAdapter
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


@pytest.fixture(name="routed_task")
def routed_task_fixture(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "gateway" / "tasks.sqlite")
    service = TaskService(store, None)
    service.started = True
    service.kick = Mock()
    task = service.submit("alice", "voice", "create", "Plan a meeting")
    task = store.update(task["id"], lambda t: t.update(status="running", checkpoint_open=True))
    endpoint = bridge.GatewayTaskEndpoint(store)
    request = SimpleNamespace(
        channel_id="video_tool", session_id=task["core_session_id"],
        request_id=task["request_id"], user_id="alice",
        params={"managed_task_binding": endpoint.binding(task)},
    )
    frames, acks = [], []

    async def acknowledge(env):
        acks.append(env)
        return await VoiceTaskServerAdapter().handle(e2a_to_agent_request(env))

    handler = SimpleNamespace(
        agent_client=SimpleNamespace(send_request=acknowledge), _stream_sessions={},
        _handle_login_credential_refresh_push=AsyncMock(return_value=False),
        publish_robot_messages=AsyncMock(),
    )

    async def push(message):
        frames.append(message)
        # Exercise the installed Gateway push boundary, including its internal-frame interception.
        await MessageHandler._handle_agent_server_push(  # pylint: disable=protected-access
            handler, build_server_push_wire(message)
        )
        return True

    monkeypatch.setattr(bridge, "send_runtime_push", push)
    server_root = tmp_path / "agent-server"
    server_root.mkdir()
    from jiuwenswarm.common import utils
    monkeypatch.setattr(utils, "get_agent_root_dir", lambda: server_root)
    yield SimpleNamespace(
        store=store, task=task, endpoint=endpoint, request=request, frames=frames,
        acks=acks, handler=handler, push=push, server_root=server_root,
    )
    endpoint.close()


async def test_checkpoint_changes_and_settlement_roundtrip_without_shared_data(routed_task):
    r = routed_task
    r.store.update(r.task["id"], lambda t: t.update(changes=[{
        "id": "change", "instruction": "Use a budget of 100", "state": "pending",
    }]))
    root = object()
    rail = SimpleNamespace(managed_tasks={})
    adapter = SimpleNamespace(
        _is_session_scoped_adapter=True,
        task_execution_binding=(SimpleNamespace(_react_agent=root), rail),
    )
    inputs, messages = {}, []
    async with bind_task_execution(r.request, adapter, inputs):
        checkpoint = rail.managed_tasks[r.request.session_id]
        ctx = SimpleNamespace(
            extra={"run_context": inputs["run"]["context"]},
            context=SimpleNamespace(add_messages=AsyncMock(side_effect=messages.append)),
            inputs=SimpleNamespace(messages=messages),
        )
        await checkpoint.before_model(ctx)
        await checkpoint.before_model(ctx)
        assert len(messages) == 1
        assert r.store.read(r.task["id"])["changes"][0]["state"] == "context_written"
        await checkpoint.after_model(ctx)
    current = r.store.read(r.task["id"])
    assert current["changes"][0]["state"] == "model_input_observed"
    assert current["execution_settled"] and not current["execution_cancelled"]
    assert not current["checkpoint_open"]
    assert not list(r.server_root.rglob("*.sqlite*"))
    assert r.acks and all(e.user_id == "alice" for e in r.acks)
    r.handler.publish_robot_messages.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("owner", "mallory"), ("session_id", "managed-task-other"), ("request_id", "stale"),
])
async def test_foreign_or_stale_execution_cannot_read_or_mutate_checkpoint(routed_task, key, value):
    r = routed_task
    remote = bridge.RemoteTaskEndpoint(r.request)
    remote.identity[key] = value
    before = r.store.read(r.task["id"])
    for action in ("status", "bind", "claim", "context_written", "observed", "close", "settle"):
        with pytest.raises(RuntimeError, match="STALE"):
            await remote.call(action)
        assert r.store.read(r.task["id"]) == before


async def test_lost_claim_ack_does_not_write_or_replay_change(routed_task, monkeypatch):
    r = routed_task
    remote = bridge.RemoteTaskEndpoint(r.request)
    await remote.call("bind")
    r.store.update(r.task["id"], lambda t: t.update(changes=[{
        "id": "change", "instruction": "French", "state": "pending",
    }]))
    saved = []

    async def lose_claim_ack(message):
        if message["payload"]["action"] == "claim":
            command = message["payload"]
            saved.append((command, r.endpoint.execute(command)))
            return True
        return await r.push(message)

    monkeypatch.setattr(bridge, "ACK_TIMEOUT", 0.02)
    monkeypatch.setattr(bridge, "send_runtime_push", lose_claim_ack)
    checkpoint = TaskCheckpoint(remote, r.task, None)
    write = AsyncMock()
    ctx = SimpleNamespace(
        extra={"run_context": {"extra": {"managed_task_request": r.task["request_id"]}}},
        context=SimpleNamespace(add_messages=write),
    )
    with pytest.raises(TimeoutError):
        await checkpoint.before_model(ctx)
    write.assert_not_called()
    assert r.store.read(r.task["id"])["changes"][0]["state"] == "claimed"
    command, receipt = saved[0]
    assert r.endpoint.execute(command) == receipt  # Repeated transport command is idempotent.
    monkeypatch.setattr(bridge, "send_runtime_push", r.push)
    await checkpoint.before_model(ctx)
    write.assert_not_called()  # A fresh claim cannot silently replay uncertain context writes.


async def test_wrong_ack_cannot_release_waiting_execution(routed_task, monkeypatch):
    r = routed_task
    captured = asyncio.Queue()

    async def capture(message):
        await captured.put(message["payload"])
        return True

    monkeypatch.setattr(bridge, "send_runtime_push", capture)
    remote = bridge.RemoteTaskEndpoint(r.request)
    pending = asyncio.create_task(remote.call("status"))
    command = await captured.get()
    ack = SimpleNamespace(
        user_id="mallory", session_id=r.request.session_id,
        params={"command_id": command["command_id"], "execution_request_id": r.request.request_id,
                "result": {"status": "running"}},
    )
    try:
        with pytest.raises(ValueError, match="identity mismatch"):
            bridge.resolve_checkpoint_ack(ack)
        assert not pending.done()
        ack.user_id = "alice"
        bridge.resolve_checkpoint_ack(ack)
        assert await pending == {"status": "running"}
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_unavailable_gateway_never_uses_an_agentserver_database(routed_task, monkeypatch):
    r = routed_task
    monkeypatch.setattr(bridge, "send_runtime_push", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="could not reach Gateway"):
        await bridge.RemoteTaskEndpoint(r.request).call("bind")
    assert not r.store.read(r.task["id"])["execution_bound"]
    assert not list(r.server_root.iterdir())


@pytest.mark.parametrize("owner", ["alice", "", "mallory"])
async def test_saved_scope_queries_routed_metadata_and_checks_owner(monkeypatch, owner):
    calls = []

    async def send(env):
        calls.append(env)
        return SimpleNamespace(ok=True, payload={"user_id": "alice"})

    manager = task_adapter.VideoSearchManager(
        None, SimpleNamespace(send_request=send), log_event=lambda _: None, qwen_active=lambda: True,
    )
    ws = SimpleNamespace(_web_connection_user_id=owner)
    if owner == "alice":
        assert await manager.scope(ws, {"search_session_id": "task-duplex:saved"}) == (
            "alice", "task-duplex:saved"
        )
    else:
        with pytest.raises(ValueError, match="unavailable"):
            await manager.scope(ws, {"search_session_id": "task-duplex:saved"})
        assert not manager.subscribers
    assert calls[0].method == ReqMethod.SESSION_GET_METADATA.value
    assert calls[0].session_id == "saved" and calls[0].user_id == (owner or None)


async def test_agent_file_query_rejects_other_owner_before_reading_history(monkeypatch):
    from jiuwenswarm.server.runtime.session import lifecycle, session_metadata, session_history
    monkeypatch.setattr(lifecycle, "guard", lambda *_: None)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *a, **k: {"user_id": "alice"})
    read = Mock()
    monkeypatch.setattr(session_history, "load_history_records", read)
    request = e2a_to_agent_request(e2a_from_agent_fields(
        request_id="files", user_id="mallory", session_id="managed-task-" + "a" * 32,
        req_method=ReqMethod.VOICE_TASK_FILES, params={"execution_request_id": "run"},
    ))
    with pytest.raises(ValueError, match="unavailable"):
        await VoiceTaskServerAdapter().handle(request)
    read.assert_not_called()


async def test_failed_remote_query_does_not_fall_back_to_gateway_session_files(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata
    local_read = Mock(side_effect=AssertionError("Gateway must not read AgentServer data"))
    monkeypatch.setattr(session_metadata, "get_session_metadata", local_read)
    client = SimpleNamespace(send_request=AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError, match="offline"):
        await task_adapter.task_agent_query(
            client, ReqMethod.SESSION_GET_METADATA, {"session_id": "saved"}, "saved", "alice"
        )
    local_read.assert_not_called()


async def test_checkpoint_ack_can_finish_while_session_is_closing(routed_task, monkeypatch):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
    from jiuwenswarm.server.runtime.session import lifecycle
    from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary

    r = routed_task
    server = object.__new__(AgentWebSocketServer)
    server._adapter_registry = AdapterRegistry()  # pylint: disable=protected-access
    server._adapter_registry.register(VoiceTaskServerAdapter())  # pylint: disable=protected-access
    server._handle_gateway_cron_callback = AsyncMock(return_value=False)  # pylint: disable=protected-access
    server._handle_lifecycle_request = AsyncMock(return_value=False)  # pylint: disable=protected-access
    guard = Mock(side_effect=lifecycle.LifecycleError("CLOSING", "Session is closing"))
    monkeypatch.setattr(lifecycle, "guard", guard)

    async def acknowledge(env):
        ws = SimpleNamespace(send=AsyncMock())
        await server._handle_message(ws, json.dumps(env.to_dict()), asyncio.Lock())  # pylint: disable=protected-access
        return parse_agent_server_wire_unary(json.loads(ws.send.call_args.args[0]))

    r.handler.agent_client.send_request = acknowledge
    remote = bridge.RemoteTaskEndpoint(r.request)
    await remote.call("bind")
    r.store.update(r.task["id"], lambda t: t.update(status="cancelling"))
    await remote.call("close")
    await remote.call("settle", cancelled=True)
    assert r.store.read(r.task["id"])["execution_cancelled"]
    guard.assert_not_called()


async def test_checkpoint_roundtrip_over_websocket_without_shared_task_database(routed_task, monkeypatch):
    """Use the production client/dispatcher; only server startup and model work are omitted."""
    from websockets.legacy.server import serve
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry

    r = routed_task
    server = object.__new__(AgentWebSocketServer)
    server._adapter_registry = AdapterRegistry()  # pylint: disable=protected-access
    server._adapter_registry.register(VoiceTaskServerAdapter())  # pylint: disable=protected-access
    server._handle_gateway_cron_callback = AsyncMock(return_value=False)  # pylint: disable=protected-access
    server._handle_lifecycle_request = AsyncMock(return_value=False)  # pylint: disable=protected-access
    connected = asyncio.get_running_loop().create_future()
    send_lock = asyncio.Lock()

    async def connection(ws):
        await ws.send(json.dumps({"type": "event", "event": "connection.ack"}))
        connected.set_result(ws)
        async for raw in ws:
            await server._handle_message(ws, raw, send_lock)  # pylint: disable=protected-access

    client = WebSocketAgentServerClient()
    r.handler.agent_client = client

    async def on_push(wire):
        await MessageHandler._handle_agent_server_push(r.handler, wire)  # pylint: disable=protected-access

    client.set_server_push_handler(on_push)

    async def push(message):
        ws = await connected
        async with send_lock:
            await ws.send(json.dumps(build_server_push_wire(message)))
        return True

    monkeypatch.setattr(bridge, "send_runtime_push", push)
    async with serve(connection, "127.0.0.1", 0) as listener:
        try:
            await client.connect(f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}")
            async with asyncio.timeout(5):
                remote = bridge.RemoteTaskEndpoint(r.request)
                await remote.call("bind")
                r.store.update(r.task["id"], lambda t: t.update(changes=[{
                    "id": "budget", "instruction": "Budget is 100", "state": "pending",
                }]))
                receipt = await remote.call("claim")
                assert receipt["changes"][0]["instruction"] == "Budget is 100"
                await remote.call("context_written", change_ids=["budget"])
                await remote.call("observed", change_ids=["budget"])
                await remote.call("close")
                await remote.call("settle", cancelled=False)
            record = r.store.read(r.task["id"])
            assert record["execution_settled"] and not record["checkpoint_open"]
            assert record["changes"][0]["state"] == "model_input_observed"
            assert not list(r.server_root.rglob("*.sqlite*"))
            r.handler.publish_robot_messages.assert_not_called()
        finally:
            await client.disconnect()
