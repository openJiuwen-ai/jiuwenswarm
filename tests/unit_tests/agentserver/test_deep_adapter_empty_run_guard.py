# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the 0-token empty-run guard (issue #1447).

A corrupted upstream interruption state makes every chat request return
instantly with 0 tokens, no output and no error. The guard must surface that
as chat.error + WARNING instead of completing silently, while legitimate
0-token exits (user cancel, HITL ask_user pending, active goal) must not be
flagged.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
import sys

import pytest

# The installed openjiuwen editable install may point at an older checkout;
# prefer the checked-out agent-core next to this repo (same trick as
# test_goal_runtime_adapter.py).
_AGENT_CORE_ROOT = Path(__file__).resolve().parents[4] / "agent-core"
if _AGENT_CORE_ROOT.is_dir() and str(_AGENT_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_CORE_ROOT))

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
