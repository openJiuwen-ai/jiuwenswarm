"""Capacity support must preserve existing PCS delivery and shared chat."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk, parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.personal_context import ws_handler as module
from jiuwenswarm.server.worker.gateway_bridge import (
    ConnHolder, WorkerGatewayBridge, begin_request_dispatch, begin_request_generation,
    end_request_dispatch, end_request_generation,
)


def config_request(request_id="pcs-config"):
    return AgentRequest(
        request_id=request_id, channel_id="web",
        req_method=ReqMethod.PERSONAL_CONTEXT_RUNTIME_GET_CONFIG, params={},
    )


def config_host():
    return SimpleNamespace(
        error_language="zh",
        get_runtime_config=AsyncMock(return_value={"configured": True, "max_context_size_mb": 75}),
    )


@pytest.mark.asyncio
async def test_normal_send_contention_delivers_original_result_after_owner_finishes():
    lock = asyncio.Lock()
    send = AsyncMock()
    await lock.acquire()
    task = asyncio.create_task(module.handle_personal_context_request(
        config_host(), SimpleNamespace(send=send), config_request(), lock,
    ))
    try:
        await asyncio.sleep(1.2)
        waiting = not task.done()
        send.assert_not_awaited()
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=2)
    assert waiting
    send.assert_awaited_once()
    result = parse_agent_server_wire_unary(json.loads(send.call_args.args[0]))
    assert result.ok and result.payload["max_context_size_mb"] == 75


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_socket_slow_pcs_send_preserves_chat_and_connection(legacy):
    if legacy:
        from websockets.legacy.client import connect
        from websockets.legacy.server import serve
    else:
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve

    release = asyncio.Event()
    finished = asyncio.Event()
    failures = []

    async def handle(socket, *_args):
        lock = asyncio.Lock()
        started = asyncio.Event()

        async def slow_send(data):
            started.set()
            await asyncio.sleep(3.3)
            await socket.send(data)

        async def chat():
            await started.wait()
            async with lock:
                await socket.send(json.dumps({"kind": "chat"}))

        wrapper = SimpleNamespace(send=slow_send, transport=socket.transport)
        try:
            results = await asyncio.gather(
                module.handle_personal_context_request(config_host(), wrapper, config_request(), lock),
                chat(), return_exceptions=True,
            )
            failures.extend(result for result in results if isinstance(result, BaseException))
        finally:
            finished.set()
        await release.wait()

    async with serve(handle, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            async with connect(f"ws://127.0.0.1:{port}", ping_interval=None) as client:
                first = json.loads(await asyncio.wait_for(client.recv(), timeout=6))
                second = json.loads(await asyncio.wait_for(client.recv(), timeout=2))
                result = parse_agent_server_wire_unary(first)
                assert result.ok and result.payload["max_context_size_mb"] == 75
                assert second == {"kind": "chat"}
                pong = await client.ping()
                await asyncio.wait_for(pong, timeout=1)
                await asyncio.wait_for(finished.wait(), timeout=1)
                assert failures == []
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_worker_slow_delivery_keeps_one_terminal_and_complete_graph(stream):
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    frames = []
    delayed = False

    async def ipc_send(frame):
        nonlocal delayed
        if not stream or frame["payload"]["sequence"] == 1:
            if not delayed:
                delayed = True
                await asyncio.sleep(3.3)
        frames.append(frame["payload"])
        bridge.acknowledge({
            "wire_id": frame["wire_id"], "generation": frame["generation"], "delivered": True,
        })

    holder.open_session(SimpleNamespace(send=ipc_send), generation=1)
    request = config_request()
    host = config_host()
    if stream:
        request = AgentRequest(
            request_id="pcs-graph", channel_id="web", is_stream=True,
            req_method=ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH, params={},
        )
        host.get_graph = AsyncMock(return_value={"context_ready": True, "nodes": [{"id": "one"}], "edges": []})
    dispatch = begin_request_dispatch(request.request_id)
    generation = begin_request_generation(1)
    try:
        await asyncio.wait_for(module.handle_personal_context_request(host, bridge, request, asyncio.Lock()), timeout=7)
        # Drain the old implementation's retained write before counting terminal frames.
        pending = tuple(getattr(module, "_BRIDGE_SEND_TASKS", ()))
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=4)
        assert sum(bool(frame["is_final"]) for frame in frames) == 1
        if stream:
            assert [frame["sequence"] for frame in frames] == [0, 1, 2]
            assert [parse_agent_server_wire_chunk(frame).payload["event_type"] for frame in frames] == [
                "personal_context.context.start", "personal_context.context.nodes", "personal_context.context.end",
            ]
        else:
            result = parse_agent_server_wire_unary(frames[0])
            assert result.ok and result.payload["max_context_size_mb"] == 75
        assert not bridge._acks
    finally:
        end_request_generation(generation)
        end_request_dispatch(dispatch)


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_keeps_chat_owner_and_does_not_abort():
    lock = asyncio.Lock()
    await lock.acquire()
    send = AsyncMock()
    abort = AsyncMock()
    ws = SimpleNamespace(send=send, transport=SimpleNamespace(abort=abort))
    task = asyncio.create_task(module._send_result(ws, config_request(), lock, {"configured": True}))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lock.locked()
        send.assert_not_awaited()
        abort.assert_not_called()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_transport_failure_releases_original_send_lock():
    lock = asyncio.Lock()
    send = AsyncMock(side_effect=OSError("test transport closed"))
    with pytest.raises(OSError):
        await module._send_result(SimpleNamespace(send=send), config_request(), lock, {"configured": True})
    assert not lock.locked()


@pytest.mark.asyncio
async def test_internal_connection_error_still_returns_localized_error():
    send = AsyncMock()
    host = config_host()
    host.get_runtime_config.side_effect = ConnectionError("internal backend unavailable")
    await module.handle_personal_context_request(host, SimpleNamespace(send=send), config_request(), asyncio.Lock())
    send.assert_awaited_once()
    response = parse_agent_server_wire_unary(json.loads(send.call_args.args[0]))
    assert not response.ok and response.payload["code"] == "INTERNAL_ERROR"
    assert "internal backend" not in response.payload["error"]
