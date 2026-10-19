# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An interrupt answer reaches the agent with the Runtime bound.

``invoke`` binds the Runtime around control delivery. ``stream`` delivered
the answer before its first execution slice, so the agent ran without a
Runtime and, for example, withdrew the Session messaging tools for the rest
of the resumed turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.context import get_current_runtime
from tests.unit_tests.runtime.test_runtime_service import (
    FakeAgent,
    FakeAgentManager,
    FakePlanController,
    _collect_events,
)


class _AskingAgent(FakeAgent):
    """Pauses on an ask_user question and records the Runtime its answer sees."""

    def __init__(self) -> None:
        self.runtime_seen_by_answer: object = "not delivered"

    async def process_message_stream(self, request: AgentRequest):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "chat.ask_user_question",
                "request_id": "question-1",
            },
        )

    async def deliver_control_input(self, request: AgentRequest):
        self.runtime_seen_by_answer = get_current_runtime()
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "chat.final", "content": "continued"},
            is_complete=True,
        )


class _Manager(FakeAgentManager):
    def get_agent_for_session_nowait(self, channel_id, session_id):
        return self.agent


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_interrupt_answer_runs_with_the_runtime_bound(streaming: bool) -> None:
    manager = _Manager()
    manager.agent = _AskingAgent()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=FakePlanController(),
    )
    session_id = f"control-context-{streaming}"
    question = AgentRequest(
        request_id="turn-1",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"query": "Ask me first", "mode": "agent"},
    )
    await _collect_events(runtime.stream(question, trigger_hook=False))

    answer = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=streaming,
        params={
            "query": "",
            "mode": "agent",
            "request_id": "question-1",
            "source": "ask_user_interrupt",
            "answers": [{"selected_options": ["yes"]}],
        },
    )
    if streaming:
        await asyncio.wait_for(
            _collect_events(runtime.stream(answer, trigger_hook=False)), timeout=5
        )
    else:
        await asyncio.wait_for(runtime.invoke(answer, trigger_hook=False), timeout=5)

    assert manager.agent.runtime_seen_by_answer is runtime
    assert get_current_runtime() is None
