"""Input steered into a running External Harness turn."""

import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import (
    DeliveryMode,
    HarnessState,
    HarnessStateError,
    SendReceipt,
)
from openjiuwen.harness_providers.inputs import harness_input_text
from openjiuwen.harness_providers.io_adapter import TURN_LIFECYCLE

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session_input import SessionInputRejectedError
from jiuwenswarm.server.runtime.agent_adapter import external_harness_helpers
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.attachments import steer_images


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
        self.queue: asyncio.Queue[OutputSchema | Exception | None] = asyncio.Queue()
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
            if isinstance(chunk, Exception):
                raise chunk
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


async def _spin(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _finished(index: int, final_output: str) -> OutputSchema:
    return OutputSchema(
        type=TURN_LIFECYCLE, index=index,
        payload={"turn_id": "turn-1", "kind": "finished", "final_output": final_output},
    )


@pytest.mark.asyncio
async def test_a_steer_that_arrives_as_the_turn_ends_becomes_the_next_turn(monkeypatch) -> None:
    # The stream has read the turn's end and saves its checkpoint; the
    # provider has not yet left RUNNING. A steer now is not sent into the
    # ended turn: it is declined once the provider is idle, so its request
    # starts the next turn.
    io = _RunningIO()
    adapter = _adapter(io)
    payloads: list[dict[str, Any]] = []
    saving = asyncio.Event()
    saved = asyncio.Event()

    async def slow_checkpoint(*_args: Any) -> None:
        saving.set()
        await saved.wait()

    monkeypatch.setattr(external_harness_helpers, "_persist_external_harness_checkpoint", slow_checkpoint)

    async def consume() -> None:
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter, _request("request-1", "first"), {"query": "first"},
        ):
            payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await io.queue.put(_finished(1, "first answer"))
    await asyncio.wait_for(saving.wait(), timeout=5)
    assert io.state is HarnessState.RUNNING

    steer = asyncio.create_task(external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "one more thing"), {"query": "one more thing"},
    ))
    await _spin()
    io.state = HarnessState.IDLE
    accepted = await asyncio.wait_for(steer, timeout=5)
    saved.set()
    await asyncio.wait_for(consumer, timeout=5)

    assert accepted is False
    io.harness.send.assert_not_awaited()
    assert io.state is HarnessState.IDLE
    assert [payload["event_type"] for payload in payloads] == ["chat.final"]


@pytest.mark.asyncio
async def test_a_steer_the_provider_takes_as_the_turn_ends_is_shown_before_the_final() -> None:
    # The provider takes the steer while the stream reads the turn's end.
    # The stream shows it before the final instead of dropping it.
    io = _RunningIO()
    adapter = _adapter(io)
    payloads: list[dict[str, Any]] = []
    sending = asyncio.Event()
    sent = asyncio.Event()

    async def slow_send(*_args: Any, **_kwargs: Any) -> SendReceipt:
        sending.set()
        await sent.wait()
        return SendReceipt(message_id="message-2", turn_id="turn-1", accepted_mode=DeliveryMode.STEER)

    io.harness.send.side_effect = slow_send

    async def consume() -> None:
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter, _request("request-1", "first"), {"query": "first"},
        ):
            payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await _spin()
    steer = asyncio.create_task(external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "one more thing"), {"query": "one more thing"},
    ))
    await asyncio.wait_for(sending.wait(), timeout=5)
    await io.queue.put(_finished(1, "first answer"))
    await _spin()
    sent.set()
    accepted = await asyncio.wait_for(steer, timeout=5)
    await asyncio.wait_for(consumer, timeout=5)

    assert accepted is True
    assert [payload["event_type"] for payload in payloads] == ["chat.input_received", "chat.final"]
    assert payloads[0]["input_request_id"] == "request-2"
    assert payloads[0]["content"] == "one more thing"
    assert adapter._external_harness_boundaries is None


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


@pytest.fixture
def steer_uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sessions = tmp_path / "sessions"
    upload_dir = sessions / "steer-session" / "uploads"
    upload_dir.mkdir(parents=True)
    monkeypatch.setattr(steer_images, "get_agent_sessions_dir", lambda: sessions)
    return upload_dir


def _image_reference(path: Path) -> dict[str, str]:
    Image.new("RGB", (32, 16), color="red").save(path, format="PNG")
    return {"type": "image", "path": str(path), "filename": path.name, "mime_type": "image/png"}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["what is on this screenshot", ""])
async def test_steered_images_reach_the_provider_and_the_input_boundary(
    steer_uploads: Path, text: str,
) -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()
    image = _image_reference(steer_uploads / "screen.png")

    accepted = await external_harness_helpers.steer_external_harness(
        adapter,
        _request("request-2", text, content=text, media_items=[image]),
        {"query": f"<prompt>{text}</prompt>"},
    )

    assert accepted is True
    sent, = io.harness.send.await_args_list
    assert sent.kwargs == {"mode": DeliveryMode.STEER}
    prompt = harness_input_text(sent.args[0])
    assert prompt.startswith(f"<prompt>{text}</prompt>\n\n")
    assert str(steer_uploads / "screen.png") in prompt
    assert '"mime_type": "image/png"' in prompt
    boundary = adapter._external_harness_boundaries.get_nowait()
    assert boundary["content"] == text
    stored, = boundary["media_items"]
    assert stored["path"] == str(steer_uploads / "screen.png")
    assert stored["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_a_steered_image_outside_the_session_uploads_is_not_sent(
    steer_uploads: Path, tmp_path: Path,
) -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()
    elsewhere = _image_reference(tmp_path / "elsewhere.png")

    with pytest.raises(ValueError, match="outside the current session uploads"):
        await external_harness_helpers.steer_external_harness(
            adapter,
            _request("request-2", "look", media_items=[elsewhere]),
            {"query": "look"},
        )
    io.harness.send.assert_not_awaited()
    assert adapter._external_harness_boundaries.empty()


@pytest.mark.asyncio
async def test_a_steer_whose_turn_ends_while_its_images_are_checked_is_declined(
    steer_uploads: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()
    image = _image_reference(steer_uploads / "screen.png")
    resolve = steer_images.resolve_steer_image_references

    def resolve_while_the_turn_ends(params, session_id):
        io.state = HarnessState.IDLE
        return resolve(params, session_id)

    monkeypatch.setattr(steer_images, "resolve_steer_image_references", resolve_while_the_turn_ends)

    assert not await external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "look", media_items=[image]), {"query": "look"},
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
    forwarded_adapter, forwarded_request, forwarded_inputs = steer.await_args.args
    assert forwarded_adapter is adapter
    assert forwarded_request.request_id == "r"
    assert forwarded_inputs == {"query": "x"}


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


async def _steer_while_the_stream_ends(end: OutputSchema | Exception | None) -> tuple[bool, list[dict[str, Any]], Any]:
    """Steer, hold the provider's send, end the stream with ``end``, then let the send return."""
    io = _RunningIO()
    adapter = _adapter(io)
    payloads: list[dict[str, Any]] = []
    sending = asyncio.Event()
    sent = asyncio.Event()

    async def slow_send(*_args: Any, **_kwargs: Any) -> SendReceipt:
        sending.set()
        await sent.wait()
        return SendReceipt(message_id="message-2", turn_id="turn-1", accepted_mode=DeliveryMode.STEER)

    io.harness.send.side_effect = slow_send

    async def consume() -> None:
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter, _request("request-1", "first"), {"query": "first"},
        ):
            payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await _spin()
    steer = asyncio.create_task(external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "one more thing"), {"query": "one more thing"},
    ))
    await asyncio.wait_for(sending.wait(), timeout=5)
    await io.queue.put(end)
    await _spin()
    sent.set()
    accepted = await asyncio.wait_for(steer, timeout=5)
    await asyncio.wait_for(consumer, timeout=5)
    return accepted, payloads, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("end", [RuntimeError("provider gone"), None], ids=["stream-failed", "stream-closed"])
async def test_a_steer_the_provider_takes_as_the_stream_breaks_is_shown_before_the_error(
    end: Exception | None,
) -> None:
    # The stream fails, or ends without the turn's end, while the provider takes
    # the steer. The steer was accepted, so the stream shows it before its error.
    accepted, payloads, adapter = await _steer_while_the_stream_ends(end)

    assert accepted is True
    assert [payload["event_type"] for payload in payloads] == ["chat.input_received", "chat.error"]
    assert payloads[0]["input_request_id"] == "request-2"
    assert adapter._external_harness_boundaries is None


@pytest.mark.asyncio
async def test_an_empty_steer_is_refused_before_it_is_sent() -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    adapter = _adapter(io)
    adapter._external_harness_boundaries = asyncio.Queue()

    for query in ("", "   ", None):
        with pytest.raises(SessionInputRejectedError):
            await external_harness_helpers.steer_external_harness(
                adapter, _request("r", ""), {"query": query},
            )
    io.harness.send.assert_not_awaited()
    assert adapter._external_harness_boundaries.empty()


@pytest.mark.asyncio
async def test_a_text_steer_records_the_text_the_provider_got() -> None:
    io = _RunningIO()
    io.state = HarnessState.RUNNING
    adapter = _adapter(io)
    boundaries: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    adapter._external_harness_boundaries = boundaries

    assert await external_harness_helpers.steer_external_harness(
        adapter, _request("r", "typed", content="typed"), {"query": "the prompt sent"},
    )

    sent = io.harness.send.await_args.args[0]
    assert harness_input_text(sent) == "the prompt sent"
    assert boundaries.get_nowait()["content"] == "the prompt sent"


@pytest.mark.asyncio
async def test_closing_the_interleaving_reads_a_failure_it_did_not_yield() -> None:
    # The output fails while a steered input is being yielded; the consumer then
    # closes the stream. The failure is read, so asyncio does not log it.
    loop = asyncio.get_running_loop()
    logged: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: logged.append(context))

    async def failing_outputs():
        raise RuntimeError("provider gone")
        yield  # pragma: no cover

    boundaries: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    boundaries.put_nowait({"input_request_id": "request-2"})
    try:
        merged = external_harness_helpers._with_input_boundaries(failing_outputs(), boundaries)
        assert await merged.__anext__() == ("input", {"input_request_id": "request-2"})
        await _spin()
        await merged.aclose()
        del merged
        gc.collect()
        await _spin()
    finally:
        loop.set_exception_handler(previous)

    assert [context.get("message") for context in logged] == []


@pytest.mark.asyncio
async def test_closing_the_stream_early_stops_reading_the_provider() -> None:
    # The consumer leaves while the stream shows a steered input, when the read
    # of the turn's next output is pending. That read stops with the stream, so
    # it cannot take the output of a later turn.
    io = _RunningIO()
    adapter = _adapter(io)
    stream = external_harness_helpers.process_external_harness_stream(
        adapter, _request("request-1", "first"), {"query": "first"},
    )
    reading = asyncio.ensure_future(stream.__anext__())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await _spin()
    assert await external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "one more thing"), {"query": "one more thing"},
    )
    first = await asyncio.wait_for(reading, timeout=5)
    assert first.payload["event_type"] == "chat.input_received"

    await stream.aclose()

    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    assert pending == []
    later = OutputSchema(type="llm_output", index=2, payload={"content": "for the next turn"})
    await io.queue.put(later)
    await _spin()
    assert io.queue.qsize() == 1 and io.queue.get_nowait() is later
    assert adapter._external_harness_boundaries is None


class _LiveAdapter:
    """The session's adapter, streaming its External Harness turn to the facade."""

    def __init__(self, io: Any) -> None:
        self._external_harness = io
        self._external_harness_binding = {
            "kind": "external_harness",
            "agent_template_name": "coding-expert",
            "provider_name": "claudecode",
        }
        self._external_harness_boundaries = None

    async def process_message_stream_impl(self, request: AgentRequest, inputs: dict[str, Any]):
        async for chunk in external_harness_helpers.process_external_harness_stream(self, request, inputs):
            yield chunk

    async def reconcile_session_mcp(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _facade_over(monkeypatch, adapter: Any) -> tuple[Any, list[dict[str, Any]]]:
    facade = JiuWenSwarm()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **kwargs: recorded.append(kwargs))
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "en"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)
    return facade, recorded


def _user_records(recorded: list[dict[str, Any]]) -> list[str]:
    return [r["content"] for r in recorded if r["role"] == "user" and r["content"]]


async def _facade_turn_with_a_slow_steer(monkeypatch):
    """Start a facade stream over a live turn and a steer whose send waits for ``sent``."""
    io = _RunningIO()
    adapter = _LiveAdapter(io)
    facade, recorded = _facade_over(monkeypatch, adapter)
    sending, sent = asyncio.Event(), asyncio.Event()

    async def slow_send(*_args: Any, **_kwargs: Any) -> SendReceipt:
        sending.set()
        await sent.wait()
        return SendReceipt(message_id="message-2", turn_id="turn-1", accepted_mode=DeliveryMode.STEER)

    io.harness.send.side_effect = slow_send
    payloads: list[dict[str, Any]] = []

    async def consume() -> None:
        async for chunk in facade.process_message_stream(_request("request-1", "first", mode="agent")):
            if isinstance(chunk.payload, dict):
                payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await _spin()
    steer = asyncio.create_task(external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "one more thing"), {"query": "one more thing"},
    ))
    await asyncio.wait_for(sending.wait(), timeout=5)
    return io, adapter, recorded, payloads, consumer, steer, sent


@pytest.mark.asyncio
async def test_a_steer_sending_when_the_client_disconnects_is_written_to_the_history(monkeypatch) -> None:
    # The client leaves while the provider takes the steer: the consumer of the
    # facade's stream is cancelled. The accepted steer is written once.
    io, adapter, recorded, _payloads, consumer, steer, sent = await _facade_turn_with_a_slow_steer(monkeypatch)

    consumer.cancel()
    await _spin()
    sent.set()
    assert await asyncio.wait_for(steer, timeout=5) is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, timeout=5)

    assert _user_records(recorded) == ["first", "one more thing"]
    assert external_harness_helpers.take_undelivered_input_boundaries(adapter, "request-1") == []


@pytest.mark.asyncio
async def test_a_steer_sending_when_the_turn_is_stopped_is_written_once(monkeypatch) -> None:
    # Stop ends the turn as aborted while the provider takes the steer. The
    # stream shows the steer before its end, and the history has it once.
    io, _adapter, recorded, payloads, consumer, steer, sent = await _facade_turn_with_a_slow_steer(monkeypatch)

    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=1, payload={"turn_id": "turn-1", "kind": "aborted"}))
    await _spin()
    sent.set()
    assert await asyncio.wait_for(steer, timeout=5) is True
    await asyncio.wait_for(consumer, timeout=5)

    assert "chat.input_received" in [payload.get("event_type") for payload in payloads]
    assert _user_records(recorded) == ["first", "one more thing"]


@pytest.mark.asyncio
async def test_a_steer_already_written_is_not_written_again_when_the_client_disconnects(monkeypatch) -> None:
    io = _RunningIO()
    adapter = _LiveAdapter(io)
    facade, recorded = _facade_over(monkeypatch, adapter)
    payloads: list[dict[str, Any]] = []

    async def consume() -> None:
        async for chunk in facade.process_message_stream(_request("request-1", "first", mode="agent")):
            if isinstance(chunk.payload, dict):
                payloads.append(chunk.payload)

    consumer = asyncio.create_task(consume())
    await _wait_streaming(adapter)
    await io.queue.put(OutputSchema(type=TURN_LIFECYCLE, index=0, payload={"turn_id": "turn-1", "kind": "started"}))
    await _spin()
    assert await external_harness_helpers.steer_external_harness(
        adapter, _request("request-2", "one more thing"), {"query": "one more thing"},
    )
    for _ in range(100):
        if "chat.input_received" in [payload.get("event_type") for payload in payloads]:
            break
        await asyncio.sleep(0.01)
    assert _user_records(recorded) == ["first", "one more thing"]
    # The same steer is handed back as undelivered too, as a racing close could.
    shown = next(payload for payload in payloads if payload.get("event_type") == "chat.input_received")
    adapter._external_harness_undelivered_inputs = {"request-1": [dict(shown)]}

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, timeout=5)

    assert _user_records(recorded) == ["first", "one more thing"]
