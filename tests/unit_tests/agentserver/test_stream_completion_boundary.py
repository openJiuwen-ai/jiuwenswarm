# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A nested stream must not terminate the facade's transport sequence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from websockets.asyncio.server import ServerConnection, serve

from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from e2a.models import E2AEnvelope
from e2a.wire_codec import encode_agent_chunk_for_wire, parse_agent_server_wire_chunk
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
from jiuwenswarm.runtime.session.model import SessionExecutionState, SessionWorkKind
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def _completion_stream(
    monkeypatch: pytest.MonkeyPatch,
    adapter: object,
    finalize: Callable[..., Awaitable[str]],
) -> tuple[interface_module.JiuWenSwarm, AgentRequest]:
    monkeypatch.setattr(
        interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_args, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        interface_module, "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "finalize_assistant_response_if_a2ui", finalize)
    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-completion-boundary",
        channel_id="web",
        session_id="sess-completion-boundary",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )
    return swarm, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nested_payload",
    [
        None,
        {"is_complete": True},
        {"event_type": "chat.final", "content": "complete answer"},
        {"event_type": "chat.error", "error": "model failed"},
        {"is_complete": True, "content": "legacy result"},
    ],
)
async def test_nested_completion_waits_for_cleanup_and_postprocessing(
    monkeypatch: pytest.MonkeyPatch,
    nested_payload: dict | None,
) -> None:
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    handoff_observed = asyncio.Event()
    postprocessing_started = asyncio.Event()
    release_postprocessing = asyncio.Event()
    postprocessing_finished = asyncio.Event()
    observed: list[AgentResponseChunk] = []
    wire_frames: list[dict] = []

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs) -> AsyncIterator[AgentResponseChunk]:
            yield AgentResponseChunk(
                request_id="req-completion-boundary",
                channel_id="web",
                payload=nested_payload,
                is_complete=True,
            )
            # Observing this later frame proves the consumer processed the
            # nested terminal marker before the assertion, without a sleep.
            yield AgentResponseChunk(
                request_id="req-completion-boundary",
                channel_id="web",
                payload={"event_type": "chat.usage_summary", "boundary_probe": True},
                is_complete=False,
            )
            await release_cleanup.wait()
            cleanup_finished.set()

    async def finalize(content: str, **_kwargs) -> str:
        postprocessing_started.set()
        await release_postprocessing.wait()
        postprocessing_finished.set()
        return content

    swarm, request = _completion_stream(monkeypatch, FakeAdapter(), finalize)
    stream = swarm.process_message_stream(request)

    async def consume() -> None:
        async for chunk in stream:
            observed.append(chunk)
            wire = encode_agent_chunk_for_wire(
                chunk, response_id=request.request_id, sequence=len(wire_frames),
            )
            wire_frames.append(wire)
            assert parse_agent_server_wire_chunk(wire).is_complete == chunk.is_complete
            if isinstance(chunk.payload, dict) and chunk.payload.get("boundary_probe"):
                handoff_observed.set()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(handoff_observed.wait(), timeout=5.0)
        assert not cleanup_finished.is_set()
        assert not any(chunk.is_complete for chunk in observed)
        assert not any(frame["is_final"] for frame in wire_frames)
        if nested_payload is not None and nested_payload != {"is_complete": True}:
            assert any(chunk.payload == nested_payload for chunk in observed)

        release_cleanup.set()
        await asyncio.wait_for(postprocessing_started.wait(), timeout=5.0)
        assert cleanup_finished.is_set()
        assert not postprocessing_finished.is_set()
        assert not any(chunk.is_complete for chunk in observed)
        assert not any(frame["is_final"] for frame in wire_frames)

        release_postprocessing.set()
        await asyncio.wait_for(consumer, timeout=5.0)
        assert postprocessing_finished.is_set()
        assert sum(chunk.is_complete for chunk in observed) == 1
        assert observed[-1].is_complete is True
        assert sum(frame["is_final"] for frame in wire_frames) == 1
        assert wire_frames[-1]["is_final"] is True
    finally:
        release_cleanup.set()
        release_postprocessing.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_gateway_keeps_receiving_until_facade_and_execution_complete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    postprocessing_started = asyncio.Event()
    release_postprocessing = asyncio.Event()
    frames_sent = asyncio.Event()
    producer_closed = asyncio.Event()

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs) -> AsyncIterator[AgentResponseChunk]:
            try:
                yield AgentResponseChunk(
                    request_id="req-completion-boundary", channel_id="web",
                    payload={"event_type": "chat.final", "content": "complete answer"},
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id="req-completion-boundary", channel_id="web",
                    payload=None, is_complete=True,
                )
                cleanup_started.set()
                await release_cleanup.wait()
            finally:
                producer_closed.set()

    async def finalize(content: str, **_kwargs) -> str:
        postprocessing_started.set()
        await release_postprocessing.wait()
        return content

    swarm, request = _completion_stream(monkeypatch, FakeAdapter(), finalize)
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session(request.session_id, "web")
    client = WebSocketAgentServerClient(ping_interval=None)
    received: list[AgentResponseChunk] = []

    async def handle(socket: ServerConnection) -> None:
        await socket.send(json.dumps({
            "type": "event", "event": "connection.ack",
            "payload": {"status": "ready", "readiness": "AGENT_READY"},
        }))
        envelope = json.loads(await socket.recv())
        assert envelope["request_id"] == request.request_id
        sequence = 0
        async for chunk in coordinator.run_stream(
            request.session_id, request.request_id, SessionWorkKind.CHAT_STREAM,
            lambda: swarm.process_message_stream(request), channel_id="web",
        ):
            wire = encode_agent_chunk_for_wire(
                chunk, response_id=request.request_id, sequence=sequence,
            )
            sequence += 1
            await socket.send(json.dumps(wire))
        frames_sent.set()
        await socket.wait_closed()

    envelope = E2AEnvelope(
        request_id=request.request_id, session_id=request.session_id,
        channel="web", method="chat.send", params=request.params, is_stream=True,
    )

    async def consume() -> None:
        async for chunk in client.send_request_stream(envelope):
            received.append(chunk)

    consumer = None
    async with serve(handle, "127.0.0.1", 0) as server:
        try:
            port = server.sockets[0].getsockname()[1]
            await client.connect(f"ws://127.0.0.1:{port}")
            consumer = asyncio.create_task(consume())
            await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
            # Exercise the real Gateway's 0.7-second trailing-frame grace.
            # A nested terminal would finish this consumer within that window.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(consumer), timeout=1.0)
            snapshot = coordinator.snapshot_session(request.session_id)
            execution = next(item for item in snapshot.executions if item.request_id == request.request_id)
            assert not execution.state.terminal
            assert not any(chunk.is_complete for chunk in received)

            release_cleanup.set()
            await asyncio.wait_for(postprocessing_started.wait(), timeout=5.0)
            assert producer_closed.is_set()
            assert not consumer.done()
            assert not any(chunk.is_complete for chunk in received)

            release_postprocessing.set()
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(frames_sent.wait(), timeout=5.0)
            snapshot = coordinator.snapshot_session(request.session_id)
            completed = next(item for item in snapshot.executions if item.execution_id == execution.execution_id)
            assert completed.state is SessionExecutionState.SUCCEEDED
            assert sum(chunk.is_complete for chunk in received) == 1
            assert received[-1].is_complete is True
            assert any(chunk.payload and chunk.payload.get("content") == "complete answer" for chunk in received)
            assert "收到无目标队列的消息" not in caplog.text
        finally:
            release_cleanup.set()
            release_postprocessing.set()
            if consumer is not None and not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await client.disconnect()
            await coordinator.close()
