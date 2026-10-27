# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the 0-token empty-run guard (issue #1447).

A corrupted upstream interruption state makes every chat request return
instantly with 0 tokens, no output and no error. The guard must surface that
as chat.error + WARNING instead of completing silently, while legitimate
0-token exits (user cancel, HITL ask_user pending, active goal, forwarded
tool events such as a Web plan-execute resume) must not be flagged.
"""
import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _adapter_ready(monkeypatch: pytest.MonkeyPatch) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace(  # pylint: disable=protected-access
        get_context_usage=lambda **_kwargs: {},
    )
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_bind_runtime_cron_context", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", lambda _tokens: None)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: None)
    monkeypatch.setattr(
        adapter,
        "_apply_model_to_react_agent",
        lambda _model, **_kwargs: None,
    )
    monkeypatch.setattr(adapter, "_mark_session_active", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_register_session_agent_task", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_unregister_session_agent_task", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_unmark_session_active", lambda _session_id, **_kwargs: None)

    async def _noop_update_runtime_config(_runtime_config):
        return None

    monkeypatch.setattr(adapter, "_update_runtime_config", _noop_update_runtime_config)

    from openjiuwen.harness import observability as harness_observability
    from jiuwenswarm.agents.harness import agent_observability as swarm_agent_observability

    monkeypatch.setattr(
        harness_observability,
        "open_agent_run_span",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        harness_observability,
        "close_agent_run_span",
        lambda _handle, **_kwargs: None,
    )
    monkeypatch.setattr(
        swarm_agent_observability,
        "sync_agent_observability",
        lambda **_kwargs: None,
    )
    return adapter


class _FakeInteractionStream:
    """Yields the given chunks, then ends — mimicking the corrupted state."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self, *, abort_active_round: bool = False) -> None:
        return None


def _install_stream(adapter: JiuWenSwarmDeepAdapter, chunks: list) -> None:
    adapter._instance.attach_output = AsyncMock(  # pylint: disable=protected-access
        return_value=_FakeInteractionStream(chunks)
    )
    adapter._instance.send_input = AsyncMock()  # pylint: disable=protected-access


def _chat_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-empty-run",
        channel_id="web",
        session_id="sess-empty-run",
        params={"query": "你好", "mode": "agent"},
        is_stream=True,
    )


def _payload_events(chunks: list) -> list[dict]:
    return [
        chunk.payload
        for chunk in chunks
        if isinstance(chunk.payload, dict)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("permission_dispatched", [False, True])
async def test_input_timestamp_precedes_notice_and_model_phase(monkeypatch, permission_dispatched):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter = _adapter_ready(monkeypatch)
    now = [1_800_000_000.0]
    monkeypatch.setattr(interface_deep.time, "time", lambda: now[0])

    async def update_runtime_config(_config):
        now[0] += 10

    monkeypatch.setattr(adapter, "_update_runtime_config", update_runtime_config)
    monkeypatch.setattr(adapter, "_build_image_tool_fallback_notice", lambda *_args, **_kwargs: {
        "event_type": "chat.notice", "notice_type": "image_tool_fallback",
        "content": "Image is not supported", "timestamp": now[0] * 1000,
    })

    async def attach_and_send(*_args, **_kwargs):
        now[0] += 10
        return _FakeInteractionStream([
            SimpleNamespace(type="session_output_phase", payload={
                "output_phase_id": "phase", "applied_input_ids": [],
            }),
            SimpleNamespace(type="llm_output", payload={"content": "answer"}),
            SimpleNamespace(type="answer", payload={"output": "answer", "result_type": "answer"}),
        ]), permission_dispatched

    monkeypatch.setattr(adapter, "_attach_and_send_inputs", attach_and_send)
    events = _payload_events([
        chunk async for chunk in adapter.process_message_stream_impl(_chat_request(), {"query": "你好"})
    ])
    notice = next(event for event in events if event["event_type"] == "chat.notice")
    phase = next(event for event in events if event["event_type"] == "chat.output_phase")
    assert phase["input_timestamp"] == 1_800_000_000_000
    assert phase["input_timestamp"] < notice["timestamp"] < phase["timestamp"]
    if permission_dispatched:
        accepted = next(event for event in events if event["event_type"] == "runtime.accepted")
        assert accepted["timestamp"] == phase["input_timestamp"]


@pytest.mark.anyio
async def test_empty_run_emits_chat_error_and_no_final(monkeypatch):
    """0-token round with no output must error loudly, not complete silently."""
    adapter = _adapter_ready(monkeypatch)
    # The corrupted deadlock: the stream ends immediately with nothing —
    # no llm_usage, no llm_output, no answer, no error.
    _install_stream(adapter, [])

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _chat_request(),
            {"query": "你好"},
        )
    ]
    events = _payload_events(chunks)

    assert {
        "event_type": "chat.error",
        "error": (
            "会话状态异常：本轮请求未调用模型且无任何输出，"
            "该会话可能已损坏，请新建会话重试。"
        ),
        "error_type": "EmptyLLMRun",
    } in events
    # The guard must suppress the synthetic stream-end success final.
    assert not any(event.get("event_type") == "chat.final" for event in events)


@pytest.mark.anyio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_moa_notices_do_not_count_as_round_output(monkeypatch, as_dict):
    adapter = _adapter_ready(monkeypatch)
    payload = {"operation_id": "notice", "phase": "reference", "status": "started"}
    notice = {"event_type": "chat.moa_status", **payload} if as_dict else SimpleNamespace(
        type="chat.moa_status", payload=payload,
    )
    _install_stream(adapter, [notice])
    observe_round = Mock()
    monkeypatch.setattr(adapter, "_track_round_output_boundary", observe_round)
    events = _payload_events([
        chunk async for chunk in adapter.process_message_stream_impl(_chat_request(), {"query": "你好"})
    ])
    observe_round.assert_not_called()
    assert any(event.get("event_type") == "chat.moa_status" for event in events)
    assert any(event.get("error_type") == "EmptyLLMRun" for event in events)
    assert not any(event.get("event_type") == "chat.final" for event in events)


@pytest.mark.anyio
@pytest.mark.parametrize("close_early", [False, True])
async def test_moa_output_consumer_owns_and_closes_progress(monkeypatch, close_early):
    from jiuwenswarm.server.runtime.moa_progress import MoaProgress, ReferenceProgressModel

    adapter = _adapter_ready(monkeypatch)
    progress = MoaProgress()
    adapter._moa_runtime = SimpleNamespace(progress=progress)
    release = asyncio.Event()
    reader_stopped = asyncio.Event()
    close_output = AsyncMock()

    async def respond(messages, tools):
        await release.wait()
        return "answer"

    original = SimpleNamespace(invoke=AsyncMock(side_effect=respond))
    model = ReferenceProgressModel(original, progress)

    class Output:
        async def __aiter__(self):
            try:
                answer = await model.invoke(messages=[])
                yield SimpleNamespace(type="llm_output", payload={"content": answer})
                yield SimpleNamespace(type="answer", payload={"output": answer, "result_type": "answer"})
            finally:
                reader_stopped.set()

        close = close_output

    monkeypatch.setattr(adapter, "_attach_and_send_inputs", AsyncMock(return_value=(Output(), False)))
    observe_round = Mock(wraps=adapter._track_round_output_boundary)
    monkeypatch.setattr(adapter, "_track_round_output_boundary", observe_round)
    stream = adapter.process_message_stream_impl(_chat_request(), {"query": "你好"})
    async for chunk in stream:
        if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.moa_status":
            assert chunk.payload["status"] == "started"
            break
    mailbox = progress.mailbox
    assert mailbox is not None
    observe_round.assert_not_called()
    if close_early:
        await asyncio.wait_for(stream.aclose(), 1)
    else:
        release.set()
        events = _payload_events([chunk async for chunk in stream])
        assert any(event.get("event_type") == "chat.final" and event.get("content") == "answer" for event in events)
        assert not any(event.get("event_type") == "chat.error" for event in events)
    original.invoke.assert_awaited_once_with(messages=[], tools=None)
    close_output.assert_awaited_once()
    assert reader_stopped.is_set()
    assert mailbox.closed and progress.mailbox is None


@pytest.mark.anyio
@pytest.mark.parametrize("close_early", [False, True])
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_moa_close_failure_preserves_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    close_early: bool,
    cancel_close: bool,
) -> None:
    from openjiuwen.harness import observability
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.moa_progress import MoaProgress

    adapter = _adapter_ready(monkeypatch)
    progress = MoaProgress()
    adapter._moa_runtime = SimpleNamespace(progress=progress)
    output = _FakeInteractionStream([
        SimpleNamespace(type="llm_output", payload={"content": "answer"}),
        SimpleNamespace(type="answer", payload={"output": "answer", "result_type": "answer"}),
    ])
    output.close = AsyncMock()
    monkeypatch.setattr(adapter, "_attach_and_send_inputs", AsyncMock(return_value=(output, False)))
    close_started = asyncio.Event()
    native_stream = progress.stream

    class FailingCloseStream:
        def __init__(self, source: AsyncIterator[Any]) -> None:
            self.source = native_stream(source)

        def __aiter__(self) -> AsyncIterator[Any]:
            return self.source

        async def aclose(self) -> None:
            await self.source.aclose()
            close_started.set()
            if cancel_close:
                await asyncio.Event().wait()
            raise RuntimeError("MoA close failed")

    monkeypatch.setattr(progress, "stream", FailingCloseStream)
    commit = AsyncMock()
    finalize = Mock(wraps=adapter._permission_dispatch.finalize)
    unregister = Mock()
    unmark = Mock()
    span_close = Mock()
    permission_cleanup = Mock(wraps=interface_deep.cleanup_permission_context)
    authorization_release = Mock(wraps=interface_deep.AUTHORIZATION_EXECUTION_CONTEXTS.release)
    monkeypatch.setattr(adapter, "_commit_round_state", commit)
    monkeypatch.setattr(adapter._permission_dispatch, "finalize", finalize)
    monkeypatch.setattr(adapter, "_unregister_session_agent_task", unregister)
    monkeypatch.setattr(adapter, "_unmark_session_active", unmark)
    monkeypatch.setattr(observability, "close_agent_run_span", span_close)
    monkeypatch.setattr(interface_deep, "cleanup_permission_context", permission_cleanup)
    monkeypatch.setattr(interface_deep.AUTHORIZATION_EXECUTION_CONTEXTS, "release", authorization_release)
    request = _chat_request()
    events = []

    async def consume() -> None:
        stream = adapter.process_message_stream_impl(request, {"query": "hello"})
        try:
            async for chunk in stream:
                if isinstance(chunk.payload, dict):
                    events.append(chunk.payload)
                    if close_early and chunk.payload.get("event_type") == "chat.delta":
                        break
        finally:
            await stream.aclose()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(close_started.wait(), 2)
        if cancel_close:
            consumer.cancel("cancel during MoA close")
            with pytest.raises(asyncio.CancelledError, match="cancel during MoA close"):
                await consumer
        else:
            await asyncio.wait_for(consumer, 2)
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    output.close.assert_awaited_once_with(abort_active_round=close_early)
    commit.assert_awaited_once_with(request.session_id)
    span_close.assert_called_once()
    finalize.assert_called_once()
    unregister.assert_called_once_with(request.session_id)
    permission_cleanup.assert_called_once()
    authorization_release.assert_called_once_with(request.session_id, request.request_id)
    unmark.assert_called_once_with(request.session_id, cleanup_rail=True)
    assert progress.mailbox is None
    assert not any(event.get("event_type") == "chat.error" for event in events)
    if cancel_close:
        assert isinstance(span_close.call_args.kwargs["exception"], asyncio.CancelledError)
    elif not close_early:
        assert any(event.get("event_type") == "chat.final" and event.get("content") == "answer" for event in events)


@pytest.mark.anyio
async def test_moa_mailbox_close_failure_preserves_dispatch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.moa_progress import MoaProgress

    adapter = _adapter_ready(monkeypatch)
    progress = MoaProgress()
    adapter._moa_runtime = SimpleNamespace(progress=progress)
    adapter._instance.has_output_stream = lambda: False
    monkeypatch.setattr(adapter, "_attach_and_send_inputs", AsyncMock(side_effect=ValueError("dispatch failed")))
    native_close = progress.close

    def fail_close() -> None:
        native_close()
        raise RuntimeError("mailbox close failed")

    monkeypatch.setattr(progress, "close", fail_close)
    finalize = Mock(wraps=adapter._permission_dispatch.finalize)
    unmark = Mock()
    monkeypatch.setattr(adapter._permission_dispatch, "finalize", finalize)
    monkeypatch.setattr(adapter, "_unmark_session_active", unmark)
    events = _payload_events([
        chunk async for chunk in adapter.process_message_stream_impl(_chat_request(), {"query": "hello"})
    ])
    assert any(event.get("error") == "dispatch failed" for event in events)
    assert not any(event.get("event_type") == "chat.final" for event in events)
    assert progress.mailbox is None
    finalize.assert_called_once()
    unmark.assert_called_once_with(_chat_request().session_id, cleanup_rail=True)


@pytest.mark.anyio
@pytest.mark.parametrize("existing_owner", [False, True])
async def test_moa_dispatch_cleanup_preserves_only_existing_output_owner(monkeypatch, existing_owner):
    from jiuwenswarm.server.runtime.moa_progress import MoaProgress

    adapter = _adapter_ready(monkeypatch)
    progress = MoaProgress()
    adapter._moa_runtime = SimpleNamespace(progress=progress)
    adapter._instance.has_output_stream = lambda: existing_owner
    mailbox = progress.open()
    if existing_owner:
        dispatch = AsyncMock(return_value=(None, False))
    else:
        dispatch = AsyncMock(side_effect=ValueError("dispatch failed"))
    monkeypatch.setattr(adapter, "_attach_and_send_inputs", dispatch)
    events = _payload_events([
        chunk async for chunk in adapter.process_message_stream_impl(_chat_request(), {"query": "你好"})
    ])
    if existing_owner:
        assert progress.mailbox is mailbox and not mailbox.closed
        assert any(event.get("event_type") == "runtime.accepted" for event in events)
        progress.close()
    else:
        assert progress.mailbox is None and mailbox.closed
        assert any(event.get("error") == "dispatch failed" for event in events)


@pytest.mark.anyio
async def test_normal_answer_does_not_trigger_guard(monkeypatch):
    """A round with model usage and output stays a normal success."""
    adapter = _adapter_ready(monkeypatch)
    _install_stream(
        adapter,
        [
            SimpleNamespace(
                type="llm_output",
                payload={"content": "你好，有什么可以帮你？"},
            ),
            SimpleNamespace(
                type="answer",
                payload={"output": "你好，有什么可以帮你？", "result_type": "answer"},
            ),
        ],
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _chat_request(),
            {"query": "你好"},
        )
    ]
    events = _payload_events(chunks)

    assert not any(event.get("error_type") == "EmptyLLMRun" for event in events)
    assert any(event.get("event_type") == "chat.final" for event in events)


@pytest.mark.anyio
async def test_cancelled_consumer_does_not_trigger_guard(monkeypatch):
    """A user-cancelled round (rail abort armed) is a legitimate 0-token exit."""
    adapter = _adapter_ready(monkeypatch)

    class _Rail:
        @staticmethod
        def is_abort_requested(session_id: str = "") -> bool:
            return True

    adapter._stream_event_rail = _Rail()  # pylint: disable=protected-access
    _install_stream(adapter, [])

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _chat_request(),
            {"query": "你好"},
        )
    ]
    events = _payload_events(chunks)

    assert not any(event.get("error_type") == "EmptyLLMRun" for event in events)


@pytest.mark.anyio
async def test_ask_user_interrupt_does_not_trigger_guard(monkeypatch):
    """A HITL ask_user round legitimately ends without a model final."""
    adapter = _adapter_ready(monkeypatch)
    _install_stream(
        adapter,
        [
            SimpleNamespace(
                type="__interaction__",
                payload={
                    "id": "ask_1",
                    "value": SimpleNamespace(
                        request_id="ask_1",
                        questions=[{"question": "继续吗?", "options": ["是", "否"]}],
                    ),
                },
            ),
        ],
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _chat_request(),
            {"query": "你好"},
        )
    ]
    events = _payload_events(chunks)

    ask_user = [
        event
        for event in events
        if event.get("event_type") == "chat.ask_user_question"
    ]
    assert ask_user, "ask_user interrupt should still reach the client"
    assert not any(event.get("error_type") == "EmptyLLMRun" for event in events)


@pytest.mark.anyio
async def test_tool_only_zero_token_round_does_not_trigger_guard(monkeypatch):
    """A 0-token round that still forwarded tool events is not a dead session.

    Web plan-execute resume runs ``exit_plan_mode`` and force-finishes without
    calling the LLM. That must complete as a normal tool-only turn, not
    EmptyLLMRun.
    """
    adapter = _adapter_ready(monkeypatch)
    _install_stream(
        adapter,
        [
            SimpleNamespace(
                type="tool_call",
                payload={
                    "tool_call": {
                        "name": "exit_plan_mode",
                        "tool_call_id": "call_1",
                        "arguments": {},
                    }
                },
            ),
            SimpleNamespace(
                type="tool_update",
                payload={"tool_update": {"content": "running"}},
            ),
            SimpleNamespace(
                type="tool_result",
                payload={
                    "tool_result": {
                        "tool_name": "exit_plan_mode",
                        "tool_call_id": "call_1",
                        "result": "Plan mode ended.",
                    }
                },
            ),
        ],
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _chat_request(),
            {"query": "你好"},
        )
    ]
    events = _payload_events(chunks)

    assert any(event.get("event_type") == "chat.tool_call" for event in events)
    assert any(event.get("event_type") == "chat.tool_result" for event in events)
    assert not any(event.get("error_type") == "EmptyLLMRun" for event in events)
    assert any(event.get("event_type") == "chat.final" for event in events)
