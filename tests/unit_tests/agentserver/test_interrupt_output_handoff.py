# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Core lease ordering with a deterministic, model-free input producer."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.harness.schema.interaction import (
    InteractionOutputStream,
    OutputLeaseManager,
)

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.output_handoff import OutputHandoff


class LeaseCore:
    """Use installed Core's actual output implementation, not mocked EOFs."""

    def __init__(self):
        self.output = OutputLeaseManager()
        self.sent = []

    async def attach_output(self):
        lease = await self.output.attach()
        return None if lease is None else InteractionOutputStream(self, lease)

    async def next_output(self, lease):
        return await self.output.next_item(lease)

    async def detach_output(self, token, *, abort_active_round):
        await self.output.detach(token)

    async def send_input(self, request):
        self.sent.append(request)
        if len(self.sent) > 1:
            await self.output.emit("continued exactly once")
            await self.output.finish_current()


def _request(source="", channel_id="process_cli"):
    return AgentRequest(
        request_id="answer" if source else "root",
        channel_id=channel_id,
        session_id="session",
        params={"source": source, "request_id": "card", "answers": []},
    )


def _adapter(core):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = core
    adapter._enable_auto_permission = False
    adapter._permission_dispatch = SimpleNamespace(release=Mock())
    adapter._goal_record_is_active = lambda: False
    return adapter


@pytest.mark.asyncio
async def test_core_finishing_lease_reproduces_lost_output_without_barrier():
    core = LeaseCore()
    old = await core.attach_output()
    await core.send_input("root")
    await core.output.finish_current()
    assert await core.attach_output() is None
    await core.send_input("answer")
    assert len(core.sent) == 2  # Accepted, but emit() discards finishing-lease output.
    assert [item async for item in old] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", ["process_cli", "tui", "web"])
@pytest.mark.parametrize(
    "source", ["ask_user_interrupt", "confirm_interrupt", "permission_interrupt"]
)
async def test_interrupt_waits_for_old_owner_and_sends_once(channel_id, source):
    core = LeaseCore()
    adapter = _adapter(core)
    old, _ = await adapter._attach_and_send_inputs(
        _request(channel_id=channel_id), {"query": "start"}, send_without_output=False
    )
    await core.output.finish_current()
    task = asyncio.create_task(
        adapter._attach_and_send_inputs(
            _request(source, channel_id), {"query": "answer"}, send_without_output=True
        )
    )
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert len(core.sent) == 1
        assert [item async for item in old] == []
        assert not task.done()  # EOF alone does not release the host's cleanup barrier.
        await old.close(abort_active_round=False)
        async with asyncio.timeout(1):
            resumed, dispatched = await task
            assert [item async for item in resumed] == ["continued exactly once"]
            await resumed.close(abort_active_round=False)
        assert dispatched is False
        assert len(core.sent) == 2
        assert not core.output.has_consumer()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await core.output.shutdown()


@pytest.mark.asyncio
async def test_cancel_during_handoff_never_delivers_answer_or_closes_other_owner():
    core = LeaseCore()
    adapter = _adapter(core)
    old, _ = await adapter._attach_and_send_inputs(
        _request(), {}, send_without_output=False
    )
    task = asyncio.create_task(
        adapter._attach_and_send_inputs(
            _request("ask_user_interrupt"), {}, send_without_output=True
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(core.sent) == 1
    assert core.output.has_consumer()
    await old.close(abort_active_round=False)


@pytest.mark.asyncio
async def test_close_failure_rejects_handoff():
    stream = AsyncMock()
    stream.close.side_effect = RuntimeError("close failed")
    holder = OutputHandoff(object(), stream)
    with pytest.raises(RuntimeError, match="close failed"):
        await holder.close()
    with pytest.raises(RuntimeError, match="Previous output owner"):
        await holder.wait()


@pytest.mark.asyncio
async def test_active_goal_keeps_ack_only_injection():
    core = LeaseCore()
    adapter = _adapter(core)
    old, _ = await adapter._attach_and_send_inputs(
        _request(), {}, send_without_output=False
    )
    adapter._goal_record_is_active = lambda: True
    async with asyncio.timeout(1):
        stream, _ = await adapter._attach_and_send_inputs(
            _request("ask_user_interrupt"), {}, send_without_output=True
        )
    assert stream is None
    assert [item async for item in old] == ["continued exactly once"]
    await old.close(abort_active_round=False)


@pytest.mark.asyncio
async def test_permission_queue_keeps_existing_output_unavailable_error():
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    core = LeaseCore()
    adapter = _adapter(core)
    old, _ = await adapter._attach_and_send_inputs(
        _request(), {}, send_without_output=False
    )
    answer = object.__new__(interface_deep.RootPermissionAnswer)
    async with asyncio.timeout(1):
        with pytest.raises(
            interface_deep.RootPermissionQueueError, match="output_unavailable"
        ):
            await adapter._attach_and_send_inputs(
                _request("permission_interrupt"),
                {interface_deep._ROOT_PERMISSION_ANSWER_KEY: answer},
                send_without_output=True,
            )
    assert len(core.sent) == 1
    await old.close(abort_active_round=False)


@pytest.mark.asyncio
async def test_send_failure_releases_new_output_and_preserves_error():
    core = LeaseCore()
    core.send_input = AsyncMock(side_effect=ValueError("dispatch failed"))
    adapter = _adapter(core)
    with pytest.raises(ValueError, match="dispatch failed"):
        await adapter._attach_and_send_inputs(_request(), {}, send_without_output=False)
    assert not core.output.has_consumer()
    assert adapter._interaction_output_handoff.drained.is_set()


@pytest.mark.parametrize("pending", [False, True])
def test_runtime_outcome_comes_from_interruption_state_not_text(pending):
    adapter = _adapter(
        SimpleNamespace(
            loop_session=SimpleNamespace(
                get_state=lambda _: SimpleNamespace(
                    interrupted_tools={"card": object()} if pending else {}
                )
            )
        )
    )
    state = adapter._stream_completion_state(had_interaction=True)
    assert state == ("suspended" if pending else "completed")
    chunk = AgentResponseChunk(
        "request",
        "process_cli",
        payload={"event_type": "chat.final", "content": ""},
        runtime_completion=state,
    )
    event = RuntimeEvent.from_agent_message(
        chunk, request_id="request", channel_id="process_cli", session_id="session"
    )
    assert event.runtime_completion == state
    assert "runtime_completion" not in event.to_dict()
    assert event.payload == chunk.payload
    assert event.metadata == {}


@pytest.mark.parametrize("fallback", [False, True])
def test_internal_completion_never_enters_wire(monkeypatch, fallback):
    from jiuwenswarm.common.e2a import wire_codec

    if fallback:
        monkeypatch.setattr(
            wire_codec,
            "e2a_response_from_agent_chunk",
            Mock(side_effect=ValueError("encode failed")),
        )
    chunk = AgentResponseChunk(
        "request",
        "tui",
        payload={"event_type": "chat.final", "content": ""},
        runtime_completion="suspended",
    )
    wire = wire_codec.encode_agent_chunk_for_wire(
        chunk, response_id="request", sequence=1
    )
    assert "runtime_completion" not in json.dumps(wire)
    if not fallback:
        decoded = wire_codec.parse_agent_server_wire_chunk(wire)
        assert decoded.payload == chunk.payload
        assert decoded.is_complete == chunk.is_complete
