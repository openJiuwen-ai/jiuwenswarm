# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for bounded stream buffering."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.session import session_history


@pytest.mark.asyncio
async def test_auto_memory_uses_live_session_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    child_adapter = object()
    extraction_started = asyncio.Event()
    captured: dict[str, object] = {}

    class RootAdapter:
        @staticmethod
        def _get_cached_session_adapter(session_id: str):
            assert session_id == "sess-auto-memory"
            return child_adapter

    async def fake_execute_auto_memory_extraction(**kwargs) -> None:
        captured.update(kwargs)
        extraction_started.set()

    monkeypatch.setattr(
        session_history,
        "read_session_history_records",
        lambda _session_id: [{"role": "user", "content": "remember this"}],
    )
    monkeypatch.setattr(
        interface_module,
        "_execute_auto_memory_extraction",
        fake_execute_auto_memory_extraction,
    )

    request = AgentRequest(
        request_id="req-auto-memory",
        channel_id="tui",
        session_id="sess-auto-memory",
        params={"project_dir": str(tmp_path), "mode": "code.normal"},
    )

    interface_module._trigger_auto_memory_extraction(
        RootAdapter(),
        request,
        "sess-auto-memory",
    )
    await asyncio.wait_for(extraction_started.wait(), timeout=1.0)

    assert captured["parent_agent"] is child_adapter


@pytest.mark.asyncio
async def test_auto_memory_keeps_adapter_without_session_cache_accessor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    adapter = object()
    extraction_started = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_execute_auto_memory_extraction(**kwargs) -> None:
        captured.update(kwargs)
        extraction_started.set()

    monkeypatch.setattr(
        session_history,
        "read_session_history_records",
        lambda _session_id: [{"role": "user", "content": "remember this"}],
    )
    monkeypatch.setattr(
        interface_module,
        "_execute_auto_memory_extraction",
        fake_execute_auto_memory_extraction,
    )

    request = AgentRequest(
        request_id="req-auto-memory-fallback",
        channel_id="tui",
        session_id="sess-auto-memory-fallback",
        params={"project_dir": str(tmp_path), "mode": "code.normal"},
    )

    interface_module._trigger_auto_memory_extraction(
        adapter,
        request,
        "sess-auto-memory-fallback",
    )
    await asyncio.wait_for(extraction_started.wait(), timeout=1.0)

    assert captured["parent_agent"] is adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload_kind", "expected_status"),
    [
        ("chat.final", "chunk", "success"),
        ("chat.error", "chunk", "error"),
        ("chat.error", "dict", "error"),
    ],
)
async def test_process_message_stream_uses_bounded_handoff_queue(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    payload_kind: str,
    expected_status: str,
) -> None:
    real_queue = asyncio.Queue
    created_queues: list[asyncio.Queue] = []
    feedback_statuses: list[str] = []

    def queue_factory(*args, **kwargs):
        queue = real_queue(*args, **kwargs)
        created_queues.append(queue)
        return queue

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            payload = {"event_type": event_type, "content": "done"}
            if payload_kind == "chunk":
                yield AgentResponseChunk(
                    request_id="req-bounded-stream",
                    channel_id="tui",
                    payload=payload,
                    is_complete=False,
                )
            else:
                yield payload

    monkeypatch.setattr(interface_module.asyncio, "Queue", queue_factory)
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, terminal_status="success": feedback_statuses.append(
            terminal_status
        ),
    )

    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-bounded-stream",
        channel_id="tui",
        session_id="sess-bounded-stream",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    stream = swarm.process_message_stream(request)
    final_chunk = await anext(stream)
    await stream.aclose()

    assert final_chunk.payload["event_type"] == event_type
    assert feedback_statuses == [expected_status]
    assert created_queues
    assert created_queues[0].maxsize == swarm.STREAM_QUEUE_MAXSIZE
    assert created_queues[0].maxsize > 0


@pytest.mark.asyncio
async def test_stream_input_error_still_completes_feedback_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_statuses: list[str] = []
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, terminal_status="success": feedback_statuses.append(
            terminal_status
        ),
    )
    swarm = interface_module.JiuWenSwarm()

    def reject_inputs(_request):
        raise interface_module._TeamPlanApprovalPayloadError("invalid approval")

    monkeypatch.setattr(swarm, "_build_inputs", reject_inputs)
    request = AgentRequest(
        request_id="req-invalid-input",
        channel_id="tui",
        session_id="sess-invalid-input",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    chunks = [chunk async for chunk in swarm.process_message_stream(request)]

    assert chunks[0].payload["event_type"] == "chat.error"
    assert feedback_statuses == ["error"]


@pytest.mark.asyncio
async def test_empty_final_keeps_accumulated_delta_for_post_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized_inputs: list[str] = []
    feedback_statuses: list[str] = []

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-empty-final",
                channel_id="tui",
                payload={"event_type": "chat.delta", "content": "complete answer"},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-empty-final",
                channel_id="tui",
                payload={"event_type": "chat.final", "content": ""},
                is_complete=False,
            )

    async def fake_finalize(content: str, **_kwargs) -> str:
        finalized_inputs.append(content)
        return f"{content} repaired"

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "finalize_assistant_response_if_a2ui", fake_finalize)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, terminal_status="success": feedback_statuses.append(
            terminal_status
        ),
    )

    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-empty-final",
        channel_id="tui",
        session_id="sess-empty-final",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    stream = swarm.process_message_stream(request)
    await anext(stream)  # accumulated delta
    await anext(stream)  # empty producer final
    repaired_final = await anext(stream)

    assert finalized_inputs == ["complete answer"]
    assert repaired_final.payload["content"] == "complete answer repaired"
    assert feedback_statuses == ["success"]
    # Closing the client at the repaired-final yield must not overwrite the
    # already-persisted success boundary with a cancellation boundary.
    await stream.aclose()
    assert feedback_statuses == ["success"]


@pytest.mark.asyncio
async def test_later_empty_final_keeps_last_nonempty_final_for_post_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized_inputs: list[str] = []

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-repeated-final",
                channel_id="tui",
                payload={"event_type": "chat.delta", "content": "draft answer"},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-repeated-final",
                channel_id="tui",
                payload={"event_type": "chat.final", "content": "authoritative answer"},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-repeated-final",
                channel_id="tui",
                payload={"event_type": "chat.final", "content": ""},
                is_complete=False,
            )

    async def fake_finalize(content: str, **_kwargs) -> str:
        finalized_inputs.append(content)
        return content

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "finalize_assistant_response_if_a2ui", fake_finalize)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, **_kwargs: None,
    )

    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-repeated-final",
        channel_id="tui",
        session_id="sess-repeated-final",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    chunks = [chunk async for chunk in swarm.process_message_stream(request)]

    assert chunks[-1].is_complete is True
    assert finalized_inputs == ["authoritative answer"]


@pytest.mark.asyncio
async def test_closing_consumer_cancels_producer_blocked_on_full_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer_finished = asyncio.Event()
    feedback_statuses: list[str] = []

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            seq = 0
            try:
                while True:
                    yield AgentResponseChunk(
                        request_id="req-disconnect",
                        channel_id="tui",
                        payload={"event_type": "chat.delta", "content": str(seq)},
                        is_complete=False,
                    )
                    seq += 1
            finally:
                producer_finished.set()

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, terminal_status="success": feedback_statuses.append(
            terminal_status
        ),
    )

    swarm = interface_module.JiuWenSwarm()
    swarm.STREAM_QUEUE_MAXSIZE = 1
    request = AgentRequest(
        request_id="req-disconnect",
        channel_id="tui",
        session_id="sess-disconnect",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )
    stream = swarm.process_message_stream(request)

    first = await anext(stream)
    assert first.payload["event_type"] == "chat.delta"
    await asyncio.wait_for(stream.aclose(), timeout=1.0)

    assert producer_finished.is_set()
    assert feedback_statuses == ["cancelled"]


@pytest.mark.asyncio
async def test_producer_close_cancellation_does_not_leave_consumer_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback_statuses: list[str] = []

    class CloseCancellingStream:
        def __init__(self) -> None:
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return AgentResponseChunk(
                request_id="req-close-cancel",
                channel_id="tui",
                payload={"event_type": "chat.delta", "content": "partial"},
                is_complete=False,
            )

        async def aclose(self) -> None:
            raise asyncio.CancelledError

    class FakeAdapter:
        @staticmethod
        def process_message_stream_impl(*_args, **_kwargs):
            return CloseCancellingStream()

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, terminal_status="success": feedback_statuses.append(
            terminal_status
        ),
    )

    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-close-cancel",
        channel_id="tui",
        session_id="sess-close-cancel",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    async def consume() -> None:
        async for _ in swarm.process_message_stream(request):
            pass

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consume(), timeout=1.0)

    assert feedback_statuses == ["cancelled"]
