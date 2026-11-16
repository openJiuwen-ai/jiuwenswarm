# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for bounded stream buffering."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_handoff_persists_tool_boundary_and_separate_expert_identities(monkeypatch, as_dict):
    identity = {"expert_id": "", "expert_type": "agent"}
    records = []
    handoff = {
        "success": True, "status": "handed_off", "plan_id": "confirmed-plan",
        "expert": {"id": "researcher", "name": "资料专家", "source": "cache"},
    }

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            events = [
                {"event_type": "chat.delta", "content": "切换前的说明"},
                {"event_type": "chat.tool_call", "tool_call_id": "plan-call", "tool_name": "symphony_plan_experts"},
                {"event_type": "chat.tool_result", "tool_call_id": "plan-call", "tool_name": "symphony_plan_experts",
                 "raw_output": handoff, "success": True},
                {"event_type": "chat.delta", "content": "专家完成原任务"},
                {"event_type": "chat.final", "content": "专家完成原任务"},
            ]
            for payload in events:
                if payload["event_type"] == "chat.tool_result":
                    identity.update(expert_id="researcher", expert_name="资料专家")
                yield payload if as_dict else AgentResponseChunk(
                    request_id="req-handoff", channel_id="desktop", payload=payload, is_complete=False,
                )

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: records.append(record))
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: dict(identity),
    )
    request = AgentRequest(
        request_id="req-handoff", channel_id="desktop", session_id="session-handoff",
        params={"query": "请专家整理资料", "mode": "agent"}, is_stream=True,
    )

    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    finals = [record for record in records if record.get("event_type") == "chat.final"]
    assert [(record["content"], record["extra"]["expert_id"]) for record in finals] == [
        ("切换前的说明", ""), ("专家完成原任务", "researcher"),
    ]
    tool_results = [record for record in records if record.get("event_type") == "chat.tool_result"]
    assert len(tool_results) == 1 and tool_results[0]["extra"]["raw_output"] == handoff
    assert chunks[-1].is_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_ask_user_boundary_persists_pending_final_text(monkeypatch, as_dict):
    records = []

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            events = [
                {"event_type": "chat.delta", "content": "出卡前正文"},
                {
                    "event_type": "chat.ask_user_question",
                    "request_id": "ask-1",
                    "questions": [{"id": "q1", "question": "继续吗？"}],
                },
            ]
            for payload in events:
                yield payload if as_dict else AgentResponseChunk(
                    request_id="req-ask-user",
                    channel_id="desktop",
                    payload=payload,
                    is_complete=False,
                )

    monkeypatch.setattr(interface_module.JiuWenSwarm, "_ensure_adapter", lambda *_a, **_k: FakeAdapter())
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: records.append(record))
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.expert.expert_service.history_expert_identity_extra",
        lambda _session_id: {"expert_id": "", "expert_type": "agent"},
    )
    request = AgentRequest(
        request_id="req-ask-user",
        channel_id="desktop",
        session_id="session-ask-user",
        params={"query": "请确认", "mode": "agent"},
        is_stream=True,
    )

    [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    finals = [record for record in records if record.get("event_type") == "chat.final"]
    assert [record["content"] for record in finals] == ["出卡前正文"]
    assert finals[0]["request_id"] == "req-ask-user"


@pytest.mark.asyncio
async def test_process_message_stream_uses_bounded_handoff_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_queue = asyncio.Queue
    created_queues: list[asyncio.Queue] = []

    def queue_factory(*args, **kwargs):
        queue = real_queue(*args, **kwargs)
        created_queues.append(queue)
        return queue

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-bounded-stream",
                channel_id="tui",
                payload={"event_type": "chat.final", "content": "done"},
                is_complete=False,
            )

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
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)

    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-bounded-stream",
        channel_id="tui",
        session_id="sess-bounded-stream",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    chunks = [chunk async for chunk in swarm.process_message_stream(request)]

    assert chunks[-1].is_complete is True
    assert created_queues
    assert created_queues[0].maxsize == swarm.STREAM_QUEUE_MAXSIZE
    assert created_queues[0].maxsize > 0


@pytest.mark.asyncio
async def test_empty_final_keeps_accumulated_delta_for_post_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized_inputs: list[str] = []

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
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)

    swarm = interface_module.JiuWenSwarm()
    request = AgentRequest(
        request_id="req-empty-final",
        channel_id="tui",
        session_id="sess-empty-final",
        params={"query": "hello", "mode": "agent"},
        is_stream=True,
    )

    chunks = [chunk async for chunk in swarm.process_message_stream(request)]

    assert chunks[-1].is_complete is True
    assert finalized_inputs == ["complete answer"]


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
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_args: None)

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


@pytest.mark.asyncio
async def test_producer_close_cancellation_does_not_leave_consumer_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
