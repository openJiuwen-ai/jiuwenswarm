"""Input steered into a running External Harness turn."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import (
    DeliveryMode,
    HarnessState,
    HarnessStateError,
    SendReceipt,
)
from openjiuwen.harness_providers.io_adapter import TURN_LIFECYCLE

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session_input import SessionInputRejectedError
from jiuwenswarm.server.runtime.agent_adapter import external_harness_helpers
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _request(request_id: str, query: str, **params: Any) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id="steer-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": query, **params},
    )


class _RunningIO:
    """A harness IO whose turn output the test feeds by hand."""

    def __init__(self, *, steers: bool = True) -> None:
        self.state = HarnessState.IDLE
        self.queue: asyncio.Queue[OutputSchema | None] = asyncio.Queue()
        self.pending_interrupt = False
        self.steers = steers
        self.harness = SimpleNamespace(
            send=AsyncMock(
                return_value=SendReceipt(
                    message_id="message-2",
                    turn_id="turn-1",
                    accepted_mode=DeliveryMode.STEER,
                )
            )
        )

    def has_pending_interrupt(self) -> bool:
        return self.pending_interrupt

    def delivery_mode(self, *, immediate: bool) -> DeliveryMode:
        if self.state is not HarnessState.RUNNING:
            return DeliveryMode.AUTO
        return DeliveryMode.STEER if immediate and self.steers else DeliveryMode.FOLLOW_UP

    async def send(self, content: Any) -> SendReceipt:
        self.state = HarnessState.RUNNING
        return SendReceipt(message_id="message-1", turn_id="turn-1", accepted_mode=DeliveryMode.AUTO)

    async def outputs(self):
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk


def _adapter(io: Any) -> SimpleNamespace:
    return SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={
            "kind": "external_harness",
            "agent_template_name": "coding-expert",
            "provider_name": "claudecode",
        },
        _external_harness_boundaries=None,
    )


async def _wait_streaming(adapter: SimpleNamespace) -> None:
    for _ in range(100):
        if adapter._external_harness_boundaries is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("the turn's stream did not start")


@pytest.mark.asyncio
async def test_steered_input_reaches_the_running_turn_and_its_stream() -> None:
    io = _RunningIO()
    adapter = _adapter(io)
    payloads: list[dict[str, Any]] = []

    async def consume() -> None:
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter, _request("request-1", "first"), {"query": "first"},
        ):
            payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await io.queue.put(OutputSchema(type="llm_output", index=1, payload={"content": "working"}))
    for _ in range(20):
        await asyncio.sleep(0)

    accepted = await external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "also do this"), {"query": "also do this"},
    )

    assert accepted is True
    sent, = io.harness.send.await_args_list
    assert sent.kwargs == {"mode": DeliveryMode.STEER}
    assert sent.args[0].content == "also do this"
    await io.queue.put(OutputSchema(type="llm_output", index=2, payload={"content": "done both"}))
    await io.queue.put(OutputSchema(
        type=TURN_LIFECYCLE, index=3,
        payload={"turn_id": "turn-1", "kind": "finished", "final_output": "done both"},
    ))
    await asyncio.wait_for(consumer, timeout=5)

    events = [payload["event_type"] for payload in payloads]
    assert events == [
        "chat.delta", "chat.input_received", "chat.output_phase", "chat.delta", "chat.final",
    ]
    received = payloads[1]
    assert received["input_request_id"] == "request-2"
    assert received["content"] == "also do this"
    assert isinstance(received["timestamp"], float)
    assert payloads[2]["applied_input_ids"] == ["request-2"]
    assert payloads[2]["output_phase_id"]
    assert adapter._external_harness_boundaries is None


@pytest.mark.asyncio
async def test_steered_input_answered_only_by_the_final_opens_its_phase() -> None:
    io = _RunningIO()
    adapter = _adapter(io)
    payloads: list[dict[str, Any]] = []

    async def consume() -> None:
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter, _request("request-1", "first"), {"query": "first"},
        ):
            payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    for _ in range(20):
        await asyncio.sleep(0)
    assert await external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "more"), {"query": "more"},
    )
    for _ in range(20):
        await asyncio.sleep(0)
    await io.queue.put(OutputSchema(
        type=TURN_LIFECYCLE, index=1,
        payload={"turn_id": "turn-1", "kind": "finished", "final_output": "answer"},
    ))
    await asyncio.wait_for(consumer, timeout=5)

    assert [payload["event_type"] for payload in payloads] == [
        "chat.input_received", "chat.output_phase", "chat.final",
    ]


@pytest.mark.asyncio
async def test_steer_is_declined_without_a_running_streamed_turn() -> None:
    idle = _RunningIO()
    assert not await external_harness_helpers.steer_external_harness(
        _adapter(idle), _request("r", "x"), {"query": "x"},
    )
    running_unstreamed = _RunningIO()
    running_unstreamed.state = HarnessState.RUNNING
    assert not await external_harness_helpers.steer_external_harness(
        _adapter(running_unstreamed), _request("r", "x"), {"query": "x"},
    )
    no_harness = _adapter(None)
    assert not await external_harness_helpers.steer_external_harness(
        no_harness, _request("r", "x"), {"query": "x"},
    )
    idle.harness.send.assert_not_awaited()
    running_unstreamed.harness.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_is_declined_when_the_provider_does_not_steer() -> None:
    io = _RunningIO(steers=False)
    io.state = HarnessState.RUNNING
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()

    assert not await external_harness_helpers.steer_external_harness(
        adapter, _request("r", "x"), {"query": "x"},
    )
    io.harness.send.assert_not_awaited()
    assert adapter._external_harness_boundaries.empty()


@pytest.mark.asyncio
async def test_steer_is_declined_when_the_turn_ends_before_the_provider_takes_it() -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    io.harness.send.side_effect = HarnessStateError("there is no active turn to steer")
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()

    assert not await external_harness_helpers.steer_external_harness(
        adapter, _request("r", "x"), {"query": "x"},
    )
    assert adapter._external_harness_boundaries.empty()


@pytest.mark.asyncio
async def test_steer_is_refused_while_the_turn_waits_for_an_answer() -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    io.pending_interrupt = True
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()

    with pytest.raises(SessionInputRejectedError):
        await external_harness_helpers.steer_external_harness(
            adapter, _request("r", "x"), {"query": "x"},
        )
    io.harness.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_input_delivery_steers_an_external_harness_session(monkeypatch) -> None:
    steer = AsyncMock(return_value=True)
    monkeypatch.setattr(external_harness_helpers, "steer_external_harness", steer)
    adapter = SimpleNamespace(
        _external_harness_binding={"kind": "external_harness"},
        _instance=None,
    )

    steered = await JiuWenSwarmDeepAdapter.deliver_active_session_input(
        adapter, _request("r", "x", input_mode="steer"), {"query": "x"},
    )
    follow_up = await JiuWenSwarmDeepAdapter.deliver_active_session_input(
        adapter, _request("r2", "y", input_mode="follow_up"), {"query": "y"},
    )

    assert steered is True
    assert follow_up is False
    steer.assert_awaited_once()


class _ReplayAdapter:
    """Replays a turn's stream as the session's adapter gives it to the facade."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads

    async def process_message_stream_impl(self, request: AgentRequest, _inputs: dict[str, Any]):
        for payload in self._payloads:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=dict(payload),
                is_complete=False,
            )

    async def reconcile_session_mcp(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_steered_turn_ends_with_its_reply_in_the_history(monkeypatch) -> None:
    io = _RunningIO()
    adapter = _adapter(io)
    payloads: list[dict[str, Any]] = []

    async def consume() -> None:
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter, _request("request-1", "first"), {"query": "first"},
        ):
            payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    for _ in range(20):
        await asyncio.sleep(0)
    assert await external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "also do this"), {"query": "also do this"},
    )
    await io.queue.put(OutputSchema(type="llm_output", index=1, payload={"content": "done both"}))
    await io.queue.put(OutputSchema(
        type=TURN_LIFECYCLE, index=2,
        payload={"turn_id": "turn-1", "kind": "finished", "final_output": "done both"},
    ))
    await asyncio.wait_for(consumer, timeout=5)

    facade = JiuWenSwarm()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(facade, "_adapter", _ReplayAdapter(payloads))
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **kwargs: recorded.append(kwargs))
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "en"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)
    streamed = [
        chunk.payload async for chunk in facade.process_message_stream(
            _request("request-1", "first", mode="agent"),
        )
        if isinstance(chunk.payload, dict)
    ]

    assert "chat.error" not in [payload.get("event_type") for payload in streamed]
    assert [(r["role"], r.get("event_type"), r["content"]) for r in recorded if r["content"]] == [
        ("user", None, "first"),
        ("user", None, "also do this"),
        ("assistant", "chat.final", "done both"),
    ]
