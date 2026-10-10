# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker ownership, delivery confirmation, and transport recovery regressions."""

from __future__ import annotations

import asyncio
import json

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


@pytest.mark.asyncio
async def test_duplicate_is_scoped_and_late_cleanup_cannot_remove_replacement():
    registry = RequestRegistry()
    first = await registry.register(request())
    other = await registry.register(request(user="other"))
    with pytest.raises(DuplicateRequestError):
        await registry.register(request())
    assert await registry.cancel("chat-1", owner=request()) is first
    assert not other.cancelled.is_set()
    await registry.complete("chat-1", tracked=first)
    replacement = await registry.register(request())
    assert await registry.complete("chat-1", tracked=first) is None
    assert registry.get("chat-1", owner=request()) is replacement


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
