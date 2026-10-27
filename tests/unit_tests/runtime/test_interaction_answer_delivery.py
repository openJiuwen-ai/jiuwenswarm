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


def _runtime(agent: _QuestionAgent) -> tuple[AgentRuntime, SessionRunAdmission]:
    manager = FakeAgentManager()
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
@pytest.mark.parametrize("path", ["unary", "streamed"])
async def test_a_cancelled_answer_keeps_the_question_pending_while_the_pause_waits(path: str) -> None:
    session_id = f"cancelled-{path}"
    agent = _QuestionAgent()
    agent.release.clear()
    runtime, admission = await _paused(session_id, agent)

    answer = _unary_answer if path == "unary" else _streamed_answer
    cancelled = asyncio.create_task(answer(runtime, session_id))
    await asyncio.wait_for(agent.started.wait(), 5)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert admission.has_pending_interaction(session_id)
    agent.release.set()
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


class _TwoQuestionAgent(FakeAgent):
    """Pauses on two questions; a resume asks again what it left unanswered."""

    def __init__(self) -> None:
        self.open = ["q-1", "q-2"]
        self.delivered: list[str] = []

    def _questions(self, request: AgentRequest):
        for question in self.open:
            yield _chunk(request, "chat.ask_user_question", request_id=question, source="ask_user_interrupt")

    async def process_message_stream(self, request: AgentRequest):
        for chunk in self._questions(request):
            yield chunk

    async def deliver_control_input(self, request: AgentRequest):
        answered = request.params["request_id"]
        self.delivered.append(answered)
        self.open.remove(answered)
        for chunk in self._questions(request):
            yield chunk
        if not self.open:
            yield _chunk(request, "chat.final", content="answered")


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("q-1", "q-2"), ("q-2", "q-1")])
async def test_an_answer_to_any_question_of_a_pause_resumes_it(order: tuple[str, str]) -> None:
    session_id = f"two-questions-{'-'.join(order)}"
    agent = _TwoQuestionAgent()
    runtime, admission = _runtime(agent)
    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "ask me", "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False))
    assert admission.pending_interactions(session_id) == {"q-1", "q-2"}

    for question in order:
        await asyncio.wait_for(runtime.invoke(AgentRequest(
            request_id=f"answer-{question}", channel_id="web", session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "", "request_id": question, "source": "ask_user_interrupt", "answers": _ANSWERS,
                    "mode": "agent", "work_mode": "work"},
        ), trigger_hook=False), 5)

    assert agent.delivered == list(order)
    assert not admission.has_pending_interaction(session_id)


def _answer(runtime: AgentRuntime, session_id: str, question: str, kind: str):
    if kind == "streamed":
        return _collect_events(runtime.stream_interaction_answer(InteractionAnswerInput(
            request_id=f"answer-{question}", channel_id="web", session_id=session_id, interaction_id=question,
            source="ask_user_interrupt", answers=_ANSWERS, mode="agent", work_mode="work",
        ), trigger_hook=False))
    request = AgentRequest(
        request_id=f"answer-{question}", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=kind == "chat",
        params={"query": "", "request_id": question, "source": "ask_user_interrupt", "answers": _ANSWERS,
                "mode": "agent", "work_mode": "work"},
    )
    if kind == "chat":
        return _collect_events(runtime.stream(request, trigger_hook=False))
    return runtime.invoke(request, trigger_hook=False)


def _waiting_executions(runtime: AgentRuntime, session_id: str) -> list:
    snapshot = runtime.get_session_snapshot(session_id)
    return [execution for execution in snapshot.executions if not execution.state.terminal]


class _StillStreamingAgent(_TwoQuestionAgent):
    def __init__(self) -> None:
        super().__init__()
        self.end_stream = asyncio.Event()

    async def process_message_stream(self, request: AgentRequest):
        for chunk in self._questions(request):
            yield chunk
        await self.end_stream.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unary", "streamed", "chat"])
async def test_an_answer_before_the_paused_stream_ends_leaves_nothing_waiting(kind: str) -> None:
    session_id = f"open-stream-{kind}"
    agent = _StillStreamingAgent()
    runtime, admission = _runtime(agent)
    turn = asyncio.create_task(_collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "ask me", "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False)))
    for _ in range(200):
        if admission.pending_interactions(session_id) == {"q-1", "q-2"}:
            break
        await asyncio.sleep(0.01)

    await asyncio.wait_for(_answer(runtime, session_id, "q-1", kind), 5)
    agent.end_stream.set()
    await asyncio.wait_for(turn, 5)
    await asyncio.wait_for(_answer(runtime, session_id, "q-2", kind), 5)

    assert agent.delivered == ["q-1", "q-2"]
    assert not _waiting_executions(runtime, session_id)
    assert not admission.has_pending_interaction(session_id)


class _SlowAnswerAgent(_TwoQuestionAgent):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def deliver_control_input(self, request: AgentRequest):
        await self.release.wait()
        async for chunk in super().deliver_control_input(request):
            yield chunk


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kinds",
    ["unary-unary", "streamed-unary", "unary-streamed", "streamed-streamed", "chat-unary", "unary-chat", "chat-chat"],
)
async def test_two_answers_of_one_pause_both_reach_the_turn(kinds: str) -> None:
    session_id = f"race-{kinds}"
    first_kind, second_kind = kinds.split("-")
    agent = _SlowAnswerAgent()
    runtime, admission = _runtime(agent)
    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "ask me", "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False))

    first = asyncio.create_task(_answer(runtime, session_id, "q-1", first_kind))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(_answer(runtime, session_id, "q-2", second_kind))
    await asyncio.sleep(0.05)
    agent.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert agent.delivered == ["q-1", "q-2"]
    assert not _waiting_executions(runtime, session_id)
    assert not admission.has_pending_interaction(session_id)


class _NoReaskAgent(_TwoQuestionAgent):
    """Pauses on two questions; answering one finishes the turn without asking the other again."""

    async def deliver_control_input(self, request: AgentRequest):
        self.delivered.append(request.params["request_id"])
        yield _chunk(request, "chat.final", content="answered")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unary", "streamed", "chat"])
async def test_an_answer_releases_the_questions_its_pause_no_longer_waits_on(kind: str) -> None:
    session_id = f"no-reask-{kind}"
    agent = _NoReaskAgent()
    runtime, admission = _runtime(agent)
    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "ask me", "mode": "agent", "work_mode": "work"},
    ), trigger_hook=False))
    assert admission.pending_interactions(session_id) == {"q-1", "q-2"}

    await asyncio.wait_for(_answer(runtime, session_id, "q-1", kind), 5)

    assert agent.delivered == ["q-1"]
    assert not _waiting_executions(runtime, session_id)
    assert not admission.has_pending_interaction(session_id)
    assert await _heartbeat_can_start(admission, session_id)
