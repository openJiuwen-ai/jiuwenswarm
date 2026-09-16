# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SDK stream ownership must end before a one-shot host closes Runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.agent_definition import RuntimeAgentDefinition
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.service import AgentRuntime


class _WaitingAgent:
    def __init__(self) -> None:
        self.closed = False
        self.enter_runtime: AgentRuntime | None = None
        self.close_runtime: AgentRuntime | None = None
        self.enter_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None

    async def process_message_stream(
        self, request: AgentRequest
    ) -> AsyncGenerator[AgentResponseChunk, None]:
        self.enter_runtime = get_current_runtime()
        self.enter_task = asyncio.current_task()
        try:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": "interaction_1",
                    "questions": [{"question": "Continue?"}],
                },
            )
            await asyncio.Event().wait()
        finally:
            self.closed = True
            self.close_runtime = get_current_runtime()
            self.close_task = asyncio.current_task()


class _FiniteAgent(_WaitingAgent):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    async def process_message_stream(
        self, request: AgentRequest
    ) -> AsyncGenerator[AgentResponseChunk, None]:
        self.enter_runtime = get_current_runtime()
        try:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.delta", "content": "hello"},
            )
            if self.fail:
                raise RuntimeError("underlying Agent failed")
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "hello"},
                is_complete=True,
            )
        finally:
            self.closed = True
            self.close_runtime = get_current_runtime()


def _runtime_with_agent(
    agent: _WaitingAgent, monkeypatch: pytest.MonkeyPatch
) -> tuple[AgentRuntime, SimpleNamespace]:
    manager = SimpleNamespace(
        create_session=AsyncMock(return_value="sdk_session"),
        begin_foreground_chat=AsyncMock(),
        end_foreground_chat=AsyncMock(),
        cancel_all_inflight_work=AsyncMock(),
        cleanup=AsyncMock(),
    )
    plan = SimpleNamespace(
        ensure_state=AsyncMock(return_value=SimpleNamespace(events=[])),
        check_post_process_exit=AsyncMock(return_value=[]),
        reset_session=Mock(),
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=plan,
    )
    # Replace only the persisted-state admission boundary. Public SDK wrapping,
    # Runtime streaming/context ownership, Agent finalization, foreground
    # cleanup and the optional Session Coordinator all execute real code.
    monkeypatch.setattr(
        runtime, "_prepare_chat_turn", AsyncMock(return_value=("code", "normal", agent))
    )
    monkeypatch.setattr(runtime, "get_session", Mock(return_value=None))
    monkeypatch.setattr(runtime, "describe_session", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_trigger_before_chat_request_hook", AsyncMock())
    return runtime, manager


def _request(session_id: str | None = None) -> AgentRequest:
    return AgentRequest(
        request_id="sdk_request",
        channel_id="process_cli",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "query": "hello",
            "mode": "agent.code.normal",
            "work_mode": "code",
            "supports_user_interaction": False,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_session", [False, True])
@pytest.mark.parametrize("close_in_other_task", [False, True])
async def test_sdk_stream_aclose_drains_real_runtime_wrapper_and_context(
    monkeypatch: pytest.MonkeyPatch,
    with_session: bool,
    close_in_other_task: bool,
) -> None:
    agent = _WaitingAgent()
    runtime, manager = _runtime_with_agent(agent, monkeypatch)
    client = InProcessRuntimeClient(runtime)
    await client.start()
    session_id = None
    if with_session:
        session_id = await client.create_or_resume_session(
            channel_id="process_cli", session_id=None
        )
    request = _request(session_id)
    retained: list[AsyncGenerator[RuntimeEvent, None]] = []
    runtime_stream = runtime.stream

    def retain_inner_stream(*args, **kwargs):
        stream = runtime_stream(*args, **kwargs)
        # Keep a strong reference: GC / loop.shutdown_asyncgens must not be
        # allowed to mask an SDK wrapper that relinquishes ownership too early.
        retained.append(stream)
        return stream

    monkeypatch.setattr(runtime, "stream", retain_inner_stream)
    stream = client.stream_agent(
        request,
        RuntimeAgentDefinition(name="sdk_agent", instructions="Answer briefly."),
    )
    try:
        event = await anext(stream)
        assert event.event_type == "chat.ask_user_question"
        assert agent.enter_runtime is runtime
        assert get_current_runtime() is None
        if close_in_other_task:
            await asyncio.wait_for(stream.aclose(), timeout=1.0)
        else:
            async with asyncio.timeout(1.0):
                await stream.aclose()

        assert agent.closed, "SDK outer aclose returned before inner Agent finally"
        assert all(inner.ag_frame is None for inner in retained)
        manager.end_foreground_chat.assert_awaited_once()
        assert agent.close_runtime is runtime
        assert get_current_runtime() is None
        if with_session:
            assert agent.close_task is agent.enter_task
        elif close_in_other_task:
            assert agent.close_task is not agent.enter_task
        else:
            assert agent.close_task is agent.enter_task
    finally:
        # Also clean up on the intentionally failing pre-fix reproduction, so
        # no test contaminates another via pending async-generator finalizers.
        await stream.aclose()
        for inner in retained:
            await inner.aclose()
        await runtime.close()


@pytest.mark.asyncio
async def test_sdk_stream_normal_exhaustion_preserves_order_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FiniteAgent()
    runtime, manager = _runtime_with_agent(agent, monkeypatch)
    client = InProcessRuntimeClient(runtime)
    stream = client.stream_agent(
        _request(),
        RuntimeAgentDefinition(name="sdk_agent", instructions="Answer briefly."),
    )
    try:
        events = [event async for event in stream]

        assert [event.event_type for event in events] == ["chat.delta", "chat.final"]
        assert [event.payload["content"] for event in events] == ["hello", "hello"]
        assert events[-1].is_complete is True
        assert agent.closed
        assert agent.enter_runtime is runtime
        assert agent.close_runtime is runtime
        manager.end_foreground_chat.assert_awaited_once()
        assert get_current_runtime() is None
    finally:
        await stream.aclose()
        await runtime.close()


@pytest.mark.asyncio
async def test_sdk_stream_underlying_failure_preserves_error_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FiniteAgent(fail=True)
    runtime, manager = _runtime_with_agent(agent, monkeypatch)
    client = InProcessRuntimeClient(runtime)
    stream = client.stream_agent(
        _request(),
        RuntimeAgentDefinition(name="sdk_agent", instructions="Answer briefly."),
    )
    try:
        events = [event async for event in stream]

        assert [event.event_type for event in events] == ["chat.delta", "runtime.error"]
        assert events[-1].payload["error"] == "underlying Agent failed"
        assert events[-1].ok is False
        assert agent.closed
        assert agent.close_runtime is runtime
        manager.end_foreground_chat.assert_awaited_once()
        assert get_current_runtime() is None
    finally:
        await stream.aclose()
        await runtime.close()
