# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker execution residency, delivery and launch identity regressions."""

from __future__ import annotations
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.request_registry import DuplicateRequestError, RequestRegistry
from jiuwenswarm.server.ipc.protocol import KIND_DELIVERY_ACK, KIND_REQUEST, dump_agent_request
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.event_store import WorkerEventStore
from jiuwenswarm.server.worker.gateway_bridge import (
    ConnHolder, WorkerGatewayBridge, begin_delivery_tracking, begin_request_dispatch,
    begin_request_generation, end_delivery_tracking, end_request_dispatch, end_request_generation,
)
from unittest.mock import AsyncMock, Mock
from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_SHUTDOWN, KIND_SHUTDOWN_DONE
from jiuwenswarm.server.supervisor import identity
from jiuwenswarm.server.worker import service, stub


def request(request_id="chat-1", *, user="owner", method=ReqMethod.CHAT_SEND):
    return AgentRequest(request_id=request_id, channel_id="web", user_id=user,
                        session_id="session-1", req_method=method, params={"query": "hello"},
                        is_stream=method == ReqMethod.CHAT_SEND)


class Connection:
    def __init__(self):
        self.frames = []
        self.sent = asyncio.Event()

    async def send(self, frame):
        self.frames.append(frame)
        self.sent.set()


_SESSION_INSPECTION_REQUESTS = [
    (ReqMethod.TEAM_LIST, {}),
    (ReqMethod.ORG_SNAPSHOT, {}),
    (ReqMethod.TEAM_SNAPSHOT, {}),
    (ReqMethod.TEAM_MEMBERS_GET, {}),
    (ReqMethod.TEAM_HISTORY_GET, {}),
    (ReqMethod.SESSION_ASSETS_LIST, {}),
    (ReqMethod.SESSION_PLAN_STATUS, {}),
    (ReqMethod.A4P_AUTHORIZATION_PENDING, {}),
    (ReqMethod.COMMAND_WORKFLOWS, {}),
    (ReqMethod.COMMAND_WORKFLOWS, {"action": "list"}),
    (ReqMethod.COMMAND_WORKFLOWS, {"action": "get_workflow"}),
    (ReqMethod.COMMAND_WORKFLOWS, {"action": "get_phase"}),
    (ReqMethod.COMMAND_WORKFLOWS, {"action": "get_agent"}),
    (ReqMethod.COMMAND_GOAL, {}),
    (ReqMethod.COMMAND_GOAL, {"action": "get"}),
    (ReqMethod.COMMAND_GOAL, {"action": " GET "}),
]


@pytest.mark.parametrize(("method", "params"), _SESSION_INSPECTION_REQUESTS)
def test_pending_session_inspection_does_not_restart_a_completed_turn(method: ReqMethod, params: dict) -> None:
    store = WorkerEventStore()
    store.register(request(), "finished-turn")
    store.append({"body": {"event_type": "chat.processing_status", "is_processing": False}}, "finished-turn")
    store.finish("finished-turn")
    query = replace(request("inspection", method=method), params=params)
    record = store.register(query, "pending-query")
    assert store.snapshot(query)["is_processing"] is False
    assert record.active is True  # Delivery and duplicate guards still own the RPC.
    assert store.snapshot(query)["dispatches"][-1]["request_id"] == "inspection"


@pytest.mark.parametrize("action", ["set", "resume", "pause", "clear"])
def test_pending_goal_control_is_still_busy(action: str) -> None:
    store = WorkerEventStore()
    control = replace(request(method=ReqMethod.COMMAND_GOAL), params={"action": action})
    store.register(control, "goal-control")
    assert store.snapshot(control)["is_processing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "real_state", [None, "succeeded", "running", "waiting_for_input", "waiting_for_control", "queued"],
)
async def test_restored_session_queries_and_observers_do_not_create_work(real_state: str | None) -> None:
    from jiuwenswarm.runtime.session.model import SessionExecutionHandle, SessionExecutionState, SessionWorkKind

    holder = ConnHolder()
    conn = Connection()
    generation = holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    executions = []
    for index in range(3):
        observer = replace(request(f"observer-{index}"), params={"attach_only": True})
        bridge.events.register(observer, f"observer-{index}")
        executions.append(SessionExecutionHandle(
            execution_id=f"observer-{index}", session_id="session-1", request_id=observer.request_id,
            generation=1, work_kind=SessionWorkKind.TEAM_REQUEST, state=SessionExecutionState.WAITING_FOR_INPUT,
        ).snapshot())
    for index, (method, params) in enumerate(_SESSION_INSPECTION_REQUESTS):
        bridge.events.register(replace(request(f"query-{index}", method=method), params=params), f"query-{index}")
    if real_state is not None:
        work = bridge.events.register(request("actual-work"), "actual-work")
        if real_state != "queued":
            executions.append(SessionExecutionHandle(
                execution_id="actual-work", session_id="session-1", request_id="actual-work", generation=1,
                work_kind=SessionWorkKind.TEAM_BACKGROUND, state=SessionExecutionState(real_state),
            ).snapshot())
            bridge.events.finish(work.dispatch_id)
    dispatcher._runtime = SimpleNamespace(get_session_snapshot=lambda _: SimpleNamespace(executions=executions))
    await dispatcher._subscribe_locked({"payload": dump_agent_request(request())}, conn, generation)
    snapshot = conn.frames[-1]["payload"]
    expected_busy = real_state not in {None, "succeeded", "waiting_for_input"}
    assert snapshot["is_processing"] is expected_busy
    assert bridge.events.snapshot(request())["is_processing"] is expected_busy
    assert len(snapshot["executions"]) == len(executions)
    assert all(bridge.events.records[f"observer-{index}"].active for index in range(3))


@pytest.mark.asyncio
@pytest.mark.parametrize("team_running", [True, False])
async def test_inspection_transport_cannot_hide_registered_background_execution(team_running: bool) -> None:
    """Team lifecycle, not a read RPC's context, owns actual background activity."""
    from jiuwenswarm.runtime.session.model import SessionExecutionHandle, SessionExecutionState, SessionWorkKind

    holder = ConnHolder()
    conn = Connection()
    generation = holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    query = request("query-with-background", method=ReqMethod.ORG_SNAPSHOT)
    bridge.events.register(query, "query-with-background")
    bridge.events.finish("query-with-background")
    execution = SessionExecutionHandle(
        execution_id="background", session_id="session-1", request_id="background", root_request_id=query.request_id,
        generation=1, work_kind=SessionWorkKind.TEAM_BACKGROUND, state=SessionExecutionState.RUNNING,
    ).snapshot()
    lifecycle = SimpleNamespace(is_running=Mock(return_value=team_running))
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(generation=1, executions=(execution,)),
        _session_runtime_lifecycle=lifecycle,
    )
    await dispatcher._subscribe_locked({"payload": dump_agent_request(request())}, conn, generation)
    assert conn.frames[-1]["payload"]["is_processing"] is team_running
    context = lifecycle.is_running.call_args.args[0]
    assert context.session_id == query.session_id
    assert context.generation == 1


@pytest.mark.asyncio
async def test_duplicate_is_scoped_and_late_cleanup_cannot_remove_replacement():
    registry = RequestRegistry()
    first = await registry.register(request())
    other = await registry.register(request(user="other"))
    with pytest.raises(DuplicateRequestError):
        await registry.register(request())
    with pytest.raises(DuplicateRequestError):
        await registry.register(replace(request(), session_id="session-2"))
    assert await registry.cancel("chat-1", owner=request()) is first
    assert not other.cancelled.is_set()
    await registry.complete("chat-1", tracked=first)
    replacement = await registry.register(request())
    assert await registry.complete("chat-1", tracked=first) is None
    assert registry.get("chat-1", owner=request()) is replacement


def test_worker_duplicate_guard_spans_caller_sessions_and_preserves_new_owner():
    store = WorkerEventStore()
    accepted = request()
    store.register(accepted, "first")
    second_session = replace(accepted, session_id="session-2")
    with pytest.raises(ValueError, match="duplicate active request_id"):
        store.register(second_session, "duplicate")
    store.register(request(user="other"), "other-caller")
    store.finish("first")
    store.register(second_session, "replacement")
    store.finish("first")
    with pytest.raises(ValueError, match="duplicate active request_id"):
        store.register(accepted, "late")
    assert store.snapshot(accepted)["dispatches"][0]["dispatch_id"] == "first"
    assert store.snapshot(second_session)["dispatches"][0]["dispatch_id"] == "replacement"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_unary_reply_waits_for_real_gateway_delivery(delivered):
    holder = ConnHolder()
    conn = Connection()
    generation = holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    token = begin_request_dispatch("dispatch-1")
    gen_token = begin_request_generation(generation)
    tracking, flags = begin_delivery_tracking()
    try:
        send = asyncio.create_task(bridge.send(json.dumps({"request_id": "create-1", "ok": True})))
        await asyncio.wait_for(conn.sent.wait(), 1)
        assert not send.done()
        assert flags == []
        bridge.acknowledge({"kind": KIND_DELIVERY_ACK, "wire_id": conn.frames[0]["wire_id"],
                            "generation": generation, "delivered": delivered})
        if delivered:
            await send
            assert flags == [True]
        else:
            with pytest.raises(ConnectionError):
                await send
            assert flags == []
    finally:
        end_delivery_tracking(tracking)
        end_request_generation(gen_token)
        end_request_dispatch(token)


@pytest.mark.asyncio
async def test_critical_reply_aborts_on_transport_detach():
    holder = ConnHolder()
    conn = Connection()
    generation = holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    token = begin_request_dispatch("dispatch-1")
    try:
        send = asyncio.create_task(bridge.send('{"request_id":"create-1","ok":true}'))
        await conn.sent.wait()
        bridge.detach(generation)
        holder.close_session()
        with pytest.raises(ConnectionError):
            await send
    finally:
        end_request_dispatch(token)


@pytest.mark.asyncio
async def test_accepted_execution_survives_detach_and_publishes_on_new_connection():
    holder = ConnHolder()
    first = Connection()
    generation = holder.open_session(first)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    entered = asyncio.Event()
    release = asyncio.Event()
    cancellations = []

    class Runtime:
        def attach_gateway_connection(self, ws, lock):
            pass

        async def on_gateway_disconnect(self, ws, remote):
            cancellations.append(remote)

        async def dispatch_parsed_request(self, ws, req, lock):
            entered.set()
            await release.wait()
            await ws.send(json.dumps({"request_id": req.request_id, "is_stream": True,
                                      "body": {"event_type": "chat.final", "execution_id": "original"}}))

    await dispatcher.bind_runtime(Runtime())
    await dispatcher.mark_ready()
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "original-dispatch",
                             "generation": generation, "payload": dump_agent_request(request())}, first)
    await asyncio.wait_for(entered.wait(), 1)
    await dispatcher.detach_gateway()
    await dispatcher.retire_generation(holder.close_session())
    second = Connection()
    new_generation = holder.open_session(second)
    release.set()
    await asyncio.wait_for(second.sent.wait(), 1)
    assert cancellations == []
    assert second.frames[0]["generation"] == new_generation
    assert second.frames[0]["dispatch_id"] == "original-dispatch"
    assert second.frames[0]["payload"]["body"]["execution_id"] == "original"
    assert await dispatcher.drain(timeout=1)


@pytest.mark.asyncio
async def test_warming_queue_survives_reconnection():
    holder = ConnHolder()
    first = Connection()
    holder.open_session(first)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "queued",
                             "payload": dump_agent_request(request())}, first)
    await asyncio.sleep(0)
    await dispatcher.retire_generation(holder.close_session())
    holder.open_session(Connection())
    invoked = []

    class Runtime:
        def attach_gateway_connection(self, ws, lock):
            pass

        async def dispatch_parsed_request(self, ws, req, lock):
            invoked.append(req.request_id)

    await dispatcher.bind_runtime(Runtime())
    await dispatcher.mark_ready()
    await asyncio.sleep(0)
    assert invoked == ["chat-1"]
    assert await dispatcher.drain(timeout=1)


@pytest.mark.asyncio
async def test_resident_gateway_binding_keeps_mailbox_gated_until_runtime_ready():
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = object.__new__(AgentWebSocketServer)
    server._session_message_service = SimpleNamespace(set_available=AsyncMock())
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    await dispatcher.bind_runtime(server)
    assert server._current_ws is bridge
    server._session_message_service.set_available.assert_not_awaited()
    await dispatcher.mark_ready()
    server._session_message_service.set_available.assert_awaited_once_with(True)


@pytest.mark.asyncio
async def test_empty_execution_snapshot_does_not_hide_accepted_warming_work():
    holder = ConnHolder()
    conn = Connection()
    generation = holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)

    class Runtime:
        def attach_gateway_connection(self, ws, lock):
            pass

        def get_session_snapshot(self, session_id):
            return SimpleNamespace(executions=())

    await dispatcher.bind_runtime(Runtime())
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "warming",
                             "payload": dump_agent_request(request())}, conn)
    await asyncio.sleep(0)
    await dispatcher._subscribe_locked({"query_id": "accepted-state", "payload": dump_agent_request(request())},
                                       conn, generation)
    state = conn.frames[-1]["payload"]
    assert state["executions"] == []
    assert state["dispatches"][0]["phase"] == "accepted"
    assert state["is_processing"] is True
    assert await dispatcher.drain(timeout=1)


def test_replay_is_bounded_and_caller_isolated_with_terminal_snapshot():
    store = WorkerEventStore(max_events=2)
    store.register(request(), "dispatch-1")
    store.register(request(user="other"), "dispatch-2")
    for i in range(3):
        store.append({"request_id": "chat-1", "is_stream": True,
                      "body": {"event_type": "chat.final" if i == 2 else "chat.delta",
                               "content": str(i), "execution_id": "exec-1"}}, "dispatch-1")
    store.finish("dispatch-1")
    snapshot = store.snapshot(request(), after=0)
    assert snapshot["history_required"] is True
    assert snapshot["dispatches"][0]["terminal"]["content"] == "2"
    assert len(store.replay(request(), after=0)) == 2
    assert store.replay(request(user="other"), after=0) == []


@pytest.mark.parametrize("attach_only", [True, False, "true", None])
def test_passive_team_subscription_is_not_session_work(attach_only):
    store = WorkerEventStore()
    observer = replace(request("observer"), params={"mode": "team", "attach_only": attach_only})
    record = store.register(observer, "observer-dispatch")
    store.append({"request_id": "observer", "body": {"event_type": "chat.delta", "content": "live"}},
                 record.dispatch_id)
    assert store.snapshot(observer)["is_processing"] is (attach_only is not True)
    assert record.active is True
    assert len(store.replay(observer, after=0)) == 1
    assert len(store.snapshot(observer)["dispatches"]) == 1
    store.register(request("actual-work"), "actual-dispatch")
    assert store.snapshot(observer)["is_processing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("real_state", [None, "succeeded", "running", "waiting_for_input", "queued"])
async def test_resume_does_not_treat_attach_execution_as_busy(real_state):
    from jiuwenswarm.runtime.session.model import SessionExecutionHandle, SessionExecutionState, SessionWorkKind

    holder = ConnHolder()
    conn = Connection()
    generation = holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    observer = replace(request("observer"), params={"mode": "team", "attach_only": True})
    observer_record = bridge.events.register(observer, "observer-dispatch")

    def execution(request_id, state):
        return SessionExecutionHandle(
            execution_id=request_id, session_id="session-1", request_id=request_id,
            root_request_id=request_id, generation=1, state=state, work_kind=SessionWorkKind.TEAM_REQUEST,
        ).snapshot()

    executions = [execution("observer", SessionExecutionState.WAITING_FOR_INPUT)]
    if real_state is not None:
        record = bridge.events.register(request("actual-work"), "actual-dispatch")
        if real_state != "queued":
            executions.append(execution("actual-work", SessionExecutionState(real_state)))
            bridge.events.finish(record.dispatch_id)
    dispatcher._runtime = SimpleNamespace(get_session_snapshot=lambda _: SimpleNamespace(executions=executions))
    await dispatcher._subscribe_locked({"payload": dump_agent_request(request())}, conn, generation)
    snapshot = conn.frames[-1]["payload"]
    assert snapshot["is_processing"] is (real_state in {"running", "queued"})
    assert observer_record.active is True
    assert observer_record.is_processing is False
    assert any(item["request_id"] == "observer" for item in snapshot["executions"])


def test_summary_turn_end_does_not_finish_owner_execution():
    store = WorkerEventStore()
    record = store.register(request(), "owner-dispatch")
    record.is_processing = True
    store.append({"request_id": "summary-round:2", "body": {
        "event_type": "org.turn_completed", "team_id": "summary-team", "request_id": "summary-round",
    }}, record.dispatch_id)
    assert record.is_processing is True
    assert record.terminal is None
    assert store.snapshot(request())["is_processing"] is True


def test_replayed_frame_retains_authoritative_session_and_app_routing():
    store = WorkerEventStore()
    accepted = request()
    accepted.metadata = {"app_id": "application-one"}
    store.register(accepted, "dispatch-1")
    wire = {"request_id": "chat-1", "is_stream": True,
            "session_id": "untrusted-event-session", "body": {"delta": "hello"}}
    live = store.append(wire, "dispatch-1")
    replay = store.replay(accepted, after=0)[0]
    assert live["payload"]["session_id"] == "session-1"
    assert replay["payload"]["session_id"] == "session-1"
    assert replay["payload"]["metadata"]["app_id"] == "application-one"
    assert wire["session_id"] == "untrusted-event-session"


def test_mailbox_push_and_execution_are_owned_by_target_session():
    from dataclasses import replace
    from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
    from jiuwenswarm.runtime.execution_telemetry import observe_execution_entry

    bridge = WorkerGatewayBridge(ConnHolder())
    source = request()
    source_record = bridge.events.register(source, "source-dispatch")
    source_record.is_processing = True
    token = begin_request_dispatch(source_record.dispatch_id)
    target = replace(source, session_id="target-session", request_id="mailbox-execution",
                     params={"session_id": "target-session"})
    try:
        queued = bridge.events.append({"request_id": target.request_id, "session_id": target.session_id,
            "channel": "web", "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
            "body": {"event_type": "chat.processing_status", "is_processing": True}}, "source-dispatch")
        with bridge.execution_scope(target):
            observe_execution_entry(target, "agent")
            # The mailbox reader still sends completion after the source
            # transport disconnects; target state must not finish the source.
            frame = bridge.events.append({"request_id": target.request_id, "session_id": target.session_id,
                "channel": "web", "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
                "body": {"event_type": "chat.final", "execution_id": "target-execution"}},
                queued["dispatch_id"])
        snapshot = bridge.events.snapshot(target)
        assert snapshot["dispatches"][0]["execution_id"] == "target-execution"
        assert snapshot["dispatches"][0]["entered_at_ns"] is not None
        assert not snapshot["is_processing"]
        assert frame["payload"]["session_id"] == target.session_id
        assert [row["payload"]["session_id"] for row in bridge.events.replay(target, after=0)] == [
            "target-session", "target-session"]
        assert bridge.events.replay(source, after=0) == []
        assert source_record.is_processing
        assert source_record.last_execution_id == ""
        assert bridge.events.snapshot(replace(target, user_id="other"))["dispatches"] == []
    finally:
        end_request_dispatch(token)


def test_unspecified_and_resolved_default_agent_have_same_scope():
    from dataclasses import replace
    from jiuwenswarm.server.ipc.protocol import request_scope

    assert request_scope(request()) == request_scope(replace(request(), agent_ref={"mode": "agent", "id": "default"}))


@pytest.mark.asyncio
async def test_warming_cancel_returns_response_without_waiting_for_runtime():
    holder = ConnHolder()
    conn = Connection()
    holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    original_send = conn.send

    async def acknowledge_send(frame):
        await original_send(frame)
        if frame.get("wire_id"):
            bridge.acknowledge({"wire_id": frame["wire_id"], "generation": frame["generation"],
                                "delivered": True})

    conn.send = acknowledge_send
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "queued",
                             "payload": dump_agent_request(request())}, conn)
    await asyncio.sleep(0)
    cancel = request("cancel-1", method=ReqMethod.CHAT_CANCEL)
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "cancel",
                             "payload": dump_agent_request(cancel)}, conn)
    await asyncio.sleep(0)
    assert dispatcher._queued == []
    serialized = json.dumps(conn.frames)
    assert "chat.interrupt_result" in serialized
    await asyncio.gather(*dispatcher._tasks)
    assert any(frame.get("dispatch_id") == "cancel" and frame.get("ok") for frame in conn.frames)
    queued = bridge.events.snapshot(request())["dispatches"][0]
    assert queued["phase"] == "completed"
    assert queued["terminal"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_runtime_failure_completes_accepted_warming_requests():
    holder = ConnHolder()
    conn = Connection()
    holder.open_session(conn)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "queued",
                             "payload": dump_agent_request(request())}, conn)
    await asyncio.sleep(0)
    await dispatcher.mark_failed("configuration failed")
    assert dispatcher._queued == []
    snapshot = bridge.events.snapshot(request())
    assert snapshot["is_processing"] is False
    assert snapshot["dispatches"][0]["terminal"]["state"] == "failed"
    assert conn.frames[-1]["error"] == "configuration failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_hello_fails", [False, True])
@pytest.mark.parametrize("disconnect_error", [OSError, service.IpcError])
async def test_resident_service_pins_identity_even_after_failed_first_hello(
    monkeypatch, first_hello_fails, disconnect_error,
):
    disk = {"identity": "launch-code"}
    hash_current_disk = Mock(side_effect=lambda: disk["identity"])
    runtime_started = asyncio.Event()
    runtime = SimpleNamespace(shutdown=AsyncMock())

    class Connection:
        def __init__(self, index):
            self.index = index
            self.frames = []
            self.closed = False

        async def send(self, frame):
            self.frames.append(frame)
            if self.index == 0 and first_hello_fails and frame["kind"] == KIND_HELLO:
                raise OSError("first Front disappeared while receiving HELLO")

        async def recv(self):
            await runtime_started.wait()
            if self.index == 0:
                raise disconnect_error("Front restarted or sent a malformed frame after Runtime loaded")
            return {"kind": KIND_SHUTDOWN}

        async def close(self):
            self.closed = True

    connections = [Connection(0), Connection(1)]

    class Server:
        def __init__(self, _path):
            self.accepted = 0
            self.started = False
            self.closed = False

        async def start(self):
            self.started = True

        async def accept(self):
            assert hash_current_disk.call_count == 1
            if self.accepted:
                disk["identity"] = "new-disk-code"
            connection = connections[self.accepted]
            self.accepted += 1
            return connection

        async def close(self):
            self.closed = True

    server = Server("unused")

    async def load_runtime(_host, _port, _signals, _dispatcher, loaded):
        loaded["runtime"] = runtime
        runtime_started.set()
        return runtime

    loader = AsyncMock(side_effect=load_runtime)
    monkeypatch.setattr(identity, "code_identity", hash_current_disk)
    monkeypatch.setattr(identity, "data_scope", lambda: "trusted-data-scope")
    monkeypatch.setattr(service, "IpcServer", lambda _path: server)
    monkeypatch.setattr(service, "_load_runtime", loader)

    # Exercise the actual service loop, HELLO, detach and explicit shutdown.
    # Fake Runtime owns no SDK resources, and the test uses no source mutation.
    await service.serve_worker("unused", host="127.0.0.1", port=0)

    hellos = [next(frame for frame in connection.frames if frame["kind"] == KIND_HELLO)
              for connection in connections]
    assert [frame["code_identity"] for frame in hellos] == ["launch-code", "launch-code"]
    assert hellos[0]["instance"] == hellos[1]["instance"]
    assert hellos[0]["pid"] == hellos[1]["pid"]
    assert hellos[0]["generation"] != hellos[1]["generation"]
    assert disk["identity"] == "new-disk-code"
    hash_current_disk.assert_called_once_with()
    loader.assert_awaited_once()
    runtime.shutdown.assert_awaited_once()
    assert server.started and server.closed
    assert all(connection.closed for connection in connections)
    assert connections[1].frames[-1] == {"kind": KIND_SHUTDOWN_DONE, "ok": True}


@pytest.mark.asyncio
async def test_standalone_hello_without_pinned_value_keeps_legacy_current_identity(monkeypatch):
    hash_current_disk = Mock(return_value="standalone-current-code")
    connection = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(identity, "code_identity", hash_current_disk)
    monkeypatch.setattr(identity, "data_scope", lambda: "trusted-data-scope")
    await service._send_hello(connection, runtime_loaded=False, generation=1, instance="standalone")
    hash_current_disk.assert_called_once_with()
    assert connection.send.await_args.args[0]["code_identity"] == "standalone-current-code"


@pytest.mark.asyncio
async def test_stub_pins_identity_across_front_reconnections(monkeypatch):
    disk = {"identity": "stub-launch-code"}
    hash_current_disk = Mock(side_effect=lambda: disk["identity"])
    frames = [[], []]
    closed = []
    index = 0

    class Connection:
        def __init__(self, position):
            self.position = position

        async def send(self, frame):
            frames[self.position].append(frame)

        async def recv(self):
            if self.position == 0:
                disk["identity"] = "stub-new-disk-code"
                raise OSError("Front detached")
            return {"kind": KIND_SHUTDOWN}

        async def close(self):
            closed.append(self.position)

    async def accept():
        nonlocal index
        connection = Connection(index)
        index += 1
        return connection

    server = SimpleNamespace(start=AsyncMock(), accept=accept, close=AsyncMock())
    monkeypatch.setattr(identity, "code_identity", hash_current_disk)
    monkeypatch.setattr(identity, "data_scope", lambda: "trusted-data-scope")
    monkeypatch.setattr(stub, "IpcServer", lambda _path: server)

    await stub.serve_stub("unused")

    hellos = [next(frame for frame in connection if frame["kind"] == KIND_HELLO) for connection in frames]
    assert [frame["code_identity"] for frame in hellos] == ["stub-launch-code", "stub-launch-code"]
    assert hellos[0]["instance"] == hellos[1]["instance"]
    hash_current_disk.assert_called_once_with()
    server.close.assert_awaited_once()
    assert closed == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("ipc_opens", [True, False])
async def test_resident_worker_startup_window_retries_without_spawning(monkeypatch, tmp_path, ipc_opens):
    from jiuwenswarm.server.lifecycle import Readiness
    from jiuwenswarm.server.supervisor import resident_lifecycle, runtime_supervisor as module

    conn = SimpleNamespace(recv=AsyncMock(return_value={
        "kind": KIND_HELLO, "protocol": module.PROTOCOL_VERSION,
        "data_scope": "scope", "deployment_scope": "deployment", "code_identity": "code", "stub": False,
        "instance": "original-worker",
    }))
    connect = AsyncMock(return_value=conn) if ipc_opens else AsyncMock(side_effect=TimeoutError("IPC stayed busy"))
    process = Mock()
    monkeypatch.setattr(module, "try_connect", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "endpoint_is_listening", lambda _: True)
    monkeypatch.setattr(module, "connect_with_retry", connect)
    monkeypatch.setattr(module, "WorkerProcess", process)
    monkeypatch.setattr(identity, "data_scope", lambda: "scope")
    monkeypatch.setattr(identity, "deployment_scope", lambda: "deployment")
    monkeypatch.setattr(identity, "code_identity", lambda: "code")
    ownership = Mock(return_value=(SimpleNamespace(pid=123), 1.0))
    monkeypatch.setattr(resident_lifecycle, "worker_process", ownership)
    supervisor = module.RuntimeSupervisor(Readiness(), socket_path=str(tmp_path / "worker"), connect_timeout=.5)
    monkeypatch.setattr(supervisor, "_cancel_side_tasks", AsyncMock())
    monkeypatch.setattr(supervisor, "_start_side_tasks", Mock())
    if ipc_opens:
        await supervisor._bring_up()
        assert supervisor._conn is conn
        assert supervisor._worker_instance == "original-worker"
        ownership.assert_called_once_with(conn.recv.return_value, str(tmp_path / "worker"), allow_legacy=True)
    else:
        with pytest.raises(TimeoutError, match="IPC stayed busy"):
            await supervisor._bring_up()
        assert supervisor._conn is None
        ownership.assert_not_called()
    process.assert_not_called()
    connect.assert_awaited_once()
    assert 0 < connect.call_args.kwargs["timeout"] <= .5
