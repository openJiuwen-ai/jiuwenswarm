# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An answer reaches a paused turn once, and the pause's question stays pending exactly while it waits.

A pending question keeps Heartbeats and cross-Session messages out of the
Session, so it must be released when nobody can answer it any more and kept
when the pause still waits.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.interaction import InteractionAnswerInput
from tests.unit_tests.runtime.test_runtime_service import (
    FakeAgent,
    FakeAgentManager,
    FakePlanController,
    _collect_events,
)


@pytest.fixture(autouse=True)
def isolated_agent_dirs(tmp_path, monkeypatch):
    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions)


def _chunk(request: AgentRequest, event_type: str, **payload: object) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request.request_id, channel_id=request.channel_id,
        payload={"event_type": event_type, **payload}, is_complete=event_type == "chat.final",
    )


class _QuestionAgent(FakeAgent):
    """Asks one question on its first message; a delivered answer finishes the turn."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.running = 0
        self.overlapped = False

    async def process_message_stream(self, request: AgentRequest):
        if request.params.get("query") == "ask me":
            yield _chunk(request, "chat.ask_user_question", request_id="q-1", source="ask_user_interrupt")
            return
        yield _chunk(request, "chat.final", content="ok")

    async def deliver_control_input(self, request: AgentRequest):
        self.running += 1
        self.overlapped = self.overlapped or self.running > 1
        self.started.set()
        try:
            await self.release.wait()
            if self.refuse:
                raise RuntimeError("answer refused")
            yield _chunk(request, "chat.final", content="answered")
        finally:
            self.running -= 1


class _Manager(FakeAgentManager):
    def get_agent_for_session_nowait(self, channel_id, session_id):
        return self.agent


def _runtime(agent: _QuestionAgent) -> tuple[AgentRuntime, SessionRunAdmission]:
    manager = _Manager()
    manager.agent = agent
    admission = SessionRunAdmission()
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(),
                           plan_controller=FakePlanController(), admission_controller=admission)
    return runtime, admission


async def _paused(session_id: str, agent: _QuestionAgent) -> tuple[AgentRuntime, SessionRunAdmission]:
    runtime, admission = _runtime(agent)
    events = await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "ask me", "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False))
    assert [event.event_type for event in events] == ["chat.ask_user_question"]
    assert admission.has_pending_interaction(session_id)
    return runtime, admission


_ANSWERS = [{"question": "choose", "selected_options": ["A"]}]


def _unary_answer(runtime: AgentRuntime, session_id: str):
    return runtime.invoke(AgentRequest(
        request_id="answer-u", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        params={"query": "", "request_id": "q-1", "source": "ask_user_interrupt", "answers": _ANSWERS,
                "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False)


def _chat_streamed_answer(runtime: AgentRuntime, session_id: str):
    return _collect_events(runtime.stream(AgentRequest(
        request_id="answer-c", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={"query": "", "request_id": "q-1", "source": "ask_user_interrupt", "answers": _ANSWERS,
                "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False))


def _streamed_answer(runtime: AgentRuntime, session_id: str):
    return _collect_events(runtime.stream_interaction_answer(InteractionAnswerInput(
        request_id="answer-s", channel_id="web", session_id=session_id, interaction_id="q-1",
        source="ask_user_interrupt", answers=_ANSWERS, mode="agent", work_mode="work",
    ), trigger_hook=False))


async def _heartbeat_can_start(admission: SessionRunAdmission, session_id: str) -> bool:
    started = await admission.try_begin_heartbeat(session_id, "heartbeat-probe")
    if started:
        await admission.end_heartbeat(session_id, "heartbeat-probe")
    return started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_streamed_answer, _unary_answer),
        (_unary_answer, _streamed_answer),
        (_chat_streamed_answer, _unary_answer),
        (_unary_answer, _chat_streamed_answer),
    ],
    ids=["streamed-then-unary", "unary-then-streamed", "chat-then-unary", "unary-then-chat"],
)
async def test_a_unary_and_a_streamed_answer_never_deliver_together(first, second) -> None:
    session_id = f"exclusive-{first.__name__}-{second.__name__}"
    agent = _QuestionAgent()
    agent.release.clear()
    runtime, admission = await _paused(session_id, agent)

    running = asyncio.create_task(first(runtime, session_id))
    await asyncio.wait_for(agent.started.wait(), 5)
    refused = asyncio.create_task(second(runtime, session_id))
    await asyncio.wait({refused}, timeout=1)
    assert not agent.overlapped
    agent.release.set()
    with pytest.raises(RuntimeError, match="already"):
        await asyncio.wait_for(refused, 5)
    await asyncio.wait_for(running, 5)

    assert not admission.has_pending_interaction(session_id)  # the question was answered once


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_new_user_turn_releases_the_question_of_the_pause_it_supersedes(streaming: bool) -> None:
    session_id = f"supersede-{streaming}"
    runtime, admission = await _paused(session_id, _QuestionAgent())

    message = AgentRequest(request_id="turn-2", channel_id="web", session_id=session_id,
                           req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
                           params={"query": "something else", "mode": "agent", "work_mode": "work"})
    if streaming:
        await asyncio.wait_for(_collect_events(runtime.stream(message, trigger_hook=False)), 5)
    else:
        await asyncio.wait_for(runtime.invoke(message, trigger_hook=False), 5)

    assert not admission.has_pending_interaction(session_id)
    assert not admission.is_session_message_blocked(session_id)
    assert await _heartbeat_can_start(admission, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer", [_unary_answer, _streamed_answer, _chat_streamed_answer], ids=["unary", "streamed", "chat"]
)
async def test_a_refused_answer_keeps_the_question_pending_while_the_pause_waits(answer) -> None:
    session_id = f"refused-{answer.__name__}"
    agent = _QuestionAgent(refuse=True)
    runtime, admission = await _paused(session_id, agent)

    with pytest.raises(RuntimeError, match="answer refused"):
        await asyncio.wait_for(answer(runtime, session_id), 5)

    assert admission.has_pending_interaction(session_id)
    assert not await _heartbeat_can_start(admission, session_id)

    agent.refuse = False
    await asyncio.wait_for(answer(runtime, session_id), 5)  # the pause still waited for it
    assert not admission.has_pending_interaction(session_id)


@pytest.mark.asyncio
async def test_a_new_user_turn_keeps_the_question_a_paused_goal_still_waits_on() -> None:
    session_id = "goal-waits"
    runtime, admission = _runtime(_QuestionAgent())
    await _collect_events(runtime.stream(AgentRequest(
        request_id="goal-1", channel_id="web", session_id=session_id, req_method=ReqMethod.COMMAND_GOAL,
        is_stream=True, params={"action": "set", "objective": "ask me", "query": "ask me",
                                "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False))
    assert admission.has_pending_interaction(session_id)

    await asyncio.wait_for(_collect_events(runtime.stream(AgentRequest(
        request_id="turn-2", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "something else", "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False)), 5)

    assert admission.has_pending_interaction(session_id)
    await asyncio.wait_for(_streamed_answer(runtime, session_id), 5)  # the goal still waited for it
    assert not admission.has_pending_interaction(session_id)
