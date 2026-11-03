# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A client's resume answers client tool calls, and resumes the pause by any of them.

The task pane answers the client tool calls of a pause in one resume that names
no question: the Runtime delivers it as control input to the paused execution
(``_deliver_control_started`` for a unary request, ``_stream_control_started``
for a streamed answer) by any answered call the pause waits on. What the pause
still waits on is read from the paused agent (``pending_interactions``); each
answered call must leave the admission's pending set, or the conversation stays
busy for Heartbeats and cross-conversation messages.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
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


def _client_request(call_id: str) -> dict:
    return {"event_type": "chat.client_tool_request", "request_id": call_id, "interrupt_id": f"intr-{call_id[-1]}",
            "source": "client_tool_interrupt", "tool_name": "read_page", "client_tool": {"input": {"call": call_id}}}


class _DocumentAgent(FakeAgent):
    """Pauses on two parallel client tool calls and keeps them until answered; ``refuse`` fails the resume."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.paused = {call_id: _client_request(call_id) for call_id in ("call-a", "call-b")}
        self.delivered: list[dict] = []

    async def pending_interactions(self, _session_id: str) -> list[dict]:
        return list(self.paused.values())

    async def process_message_stream(self, request: AgentRequest):
        for payload in self.paused.values():
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id, payload=payload)

    async def deliver_control_input(self, request: AgentRequest):
        self.delivered.append(dict(request.params))
        if self.refuse:
            raise RuntimeError("client tool resume refused")
        for answer in request.params["answers"]:
            self.paused.pop(answer["request_id"], None)
        yield AgentResponseChunk(
            request_id=request.request_id, channel_id=request.channel_id,
            payload={"event_type": "chat.final", "content": "done"}, is_complete=True,
        )


def _runtime(agent: FakeAgent) -> tuple[AgentRuntime, SessionRunAdmission]:
    manager = FakeAgentManager()
    manager.agent = agent
    admission = SessionRunAdmission()
    return AgentRuntime(agent_manager=manager, initializer=AsyncMock(),
                        plan_controller=FakePlanController(), admission_controller=admission), admission


async def _paused(session_id: str, agent: _DocumentAgent) -> tuple[AgentRuntime, SessionRunAdmission]:
    runtime, admission = _runtime(agent)
    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "read both pages", "mode": "agent"},
    ), trigger_hook=False))
    assert [(p["request_id"], p["interrupt_id"]) for p in await runtime.pending_interactions(session_id)] == [
        ("call-a", "intr-a"), ("call-b", "intr-b")]
    assert admission.has_pending_interaction(session_id)
    return runtime, admission


_ANSWERS = [{"interrupt_id": "intr-a", "request_id": "call-a", "result": {"text": "one"}},
            {"interrupt_id": "intr-b", "request_id": "call-b", "result": {"text": "two"}}]


def _resume(session_id: str, *, is_stream: bool, answers: list[dict] = _ANSWERS) -> AgentRequest:
    return AgentRequest(
        request_id="resume-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=is_stream, params={"source": "client_tool_interrupt", "answers": answers},
    )


def _typed_resume(session_id: str) -> InteractionAnswerInput:
    return InteractionAnswerInput(
        request_id="resume-1", channel_id="web", session_id=session_id, interaction_id="call-b",
        source="client_tool_interrupt", answers=_ANSWERS, mode="agent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["invoke", "stream", "interaction_answer"])
async def test_a_resume_naming_no_question_answers_every_client_tool_call(path: str) -> None:
    session_id = f"client-resume-{path}"
    agent = _DocumentAgent()
    runtime, admission = await _paused(session_id, agent)

    if path == "invoke":  # unary: _deliver_control_started
        await asyncio.wait_for(runtime.invoke(_resume(session_id, is_stream=False), trigger_hook=False), 5)
    elif path == "stream":  # streamed chat.send: _stream_control_started
        await asyncio.wait_for(_collect_events(runtime.stream(_resume(session_id, is_stream=True),
                                                              trigger_hook=False)), 5)
    else:  # a typed answer: _stream_control_started
        await asyncio.wait_for(_collect_events(runtime.stream_interaction_answer(
            _typed_resume(session_id), trigger_hook=False)), 5)

    assert [answer["request_id"] for answer in agent.delivered[0]["answers"]] == ["call-a", "call-b"]
    assert await runtime.pending_interactions(session_id) == []  # nothing to send again on a later bind
    assert not admission.has_pending_interaction(session_id)  # Heartbeats and messages may enter
    assert not admission.is_session_message_blocked(session_id)


@pytest.mark.asyncio
async def test_a_session_awaits_answers_while_its_pause_waits_and_no_longer_once_resumed() -> None:
    runtime, _admission = await _paused("client-awaiting", _DocumentAgent())
    assert runtime.sessions_awaiting_answers() == ("client-awaiting",)

    await asyncio.wait_for(_collect_events(runtime.stream(_resume("client-awaiting", is_stream=True),
                                                          trigger_hook=False)), 5)

    assert runtime.sessions_awaiting_answers() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["invoke", "interaction_answer"])
async def test_a_refused_resume_keeps_every_call_it_answered_pending(path: str) -> None:
    session_id = f"client-refused-{path}"
    runtime, admission = await _paused(session_id, _DocumentAgent(refuse=True))

    with pytest.raises(RuntimeError, match="refused"):
        if path == "invoke":
            await asyncio.wait_for(runtime.invoke(_resume(session_id, is_stream=False), trigger_hook=False), 5)
        else:
            await asyncio.wait_for(_collect_events(runtime.stream_interaction_answer(
                _typed_resume(session_id), trigger_hook=False)), 5)

    # The pause still waits: both requests stay pending and are sent again on a bind.
    assert [p["request_id"] for p in await runtime.pending_interactions(session_id)] == ["call-a", "call-b"]
    assert admission.has_pending_interaction(session_id)
    await runtime.cancel_all_inflight_work(reason="test teardown")


def _deliver(runtime: AgentRuntime, request: AgentRequest):
    if request.is_stream:
        return _collect_events(runtime.stream(request, trigger_hook=False))
    return runtime.invoke(request, trigger_hook=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [False, True])
async def test_a_resume_no_paused_call_waits_on_is_refused_as_a_mismatch(is_stream: bool) -> None:
    session_id = f"client-stale-{is_stream}"
    runtime, _admission = await _paused(session_id, _DocumentAgent())
    await asyncio.wait_for(_deliver(runtime, _resume(session_id, is_stream=is_stream)), 5)
    assert runtime.resumed_interactions(_resume(session_id, is_stream=is_stream)) == {"call-a", "call-b"}

    with pytest.raises(ClientToolAnswerMismatch):
        await asyncio.wait_for(_deliver(runtime, _resume(session_id, is_stream=is_stream)), 5)


class _SlowDocumentAgent(_DocumentAgent):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def deliver_control_input(self, request: AgentRequest):
        await self.release.wait()
        async for chunk in super().deliver_control_input(request):
            yield chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [False, True])
async def test_the_same_results_sent_again_meanwhile_are_refused_as_a_mismatch_at_once(is_stream: bool) -> None:
    session_id = f"client-twice-{is_stream}"
    agent = _SlowDocumentAgent()
    runtime, _admission = await _paused(session_id, agent)
    first = asyncio.create_task(_deliver(runtime, _resume(session_id, is_stream=is_stream)))
    await asyncio.sleep(0.05)

    with pytest.raises(ClientToolAnswerMismatch):
        await asyncio.wait_for(_deliver(runtime, _resume(session_id, is_stream=is_stream)), 1)
    agent.release.set()
    await asyncio.wait_for(first, 5)
    assert len(agent.delivered) == 1


class _ProgressAgent(_DocumentAgent):
    """Streams progress, then holds the resumed turn open until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def deliver_control_input(self, request: AgentRequest):
        self.delivered.append(dict(request.params))
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                 payload={"event_type": "chat.delta", "content": "working"})
        await self.release.wait()
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                 payload={"event_type": "chat.final", "content": "done"}, is_complete=True)


@pytest.mark.asyncio
async def test_a_streamed_resume_sends_each_event_while_the_turn_runs() -> None:
    session_id = "client-streamed"
    agent = _ProgressAgent()
    runtime, _admission = await _paused(session_id, agent)

    events = runtime.stream(_resume(session_id, is_stream=True), trigger_hook=False)
    first = await asyncio.wait_for(anext(events), 5)
    assert first.payload["event_type"] == "chat.delta"
    assert not agent.release.is_set()
    agent.release.set()
    assert [event.payload["event_type"] async for event in events][-1] == "chat.final"


@pytest.mark.asyncio
async def test_a_call_whose_results_are_being_delivered_is_not_pending() -> None:
    session_id = "client-delivering"
    agent = _SlowDocumentAgent()
    runtime, _admission = await _paused(session_id, agent)
    first = asyncio.create_task(_deliver(runtime, _resume(session_id, is_stream=False, answers=_ANSWERS[:1])))
    await asyncio.sleep(0.05)

    assert [p["request_id"] for p in await runtime.pending_interactions(session_id)] == ["call-b"]
    agent.release.set()
    await asyncio.wait_for(first, 5)


class _PartialResumeAgent(_DocumentAgent):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def deliver_control_input(self, request: AgentRequest):
        self.delivered.append(dict(request.params))
        await self.gate.wait()
        for answer in request.params["answers"]:
            self.paused.pop(answer["request_id"], None)
        for payload in self.paused.values():
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id, payload=payload)
        if not self.paused:
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                     payload={"event_type": "chat.final", "content": "done"}, is_complete=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [False, True])
async def test_a_resume_overlapping_a_partial_one_resumes_the_call_left_waiting(is_stream: bool) -> None:
    session_id = f"client-overlap-{is_stream}"
    agent = _PartialResumeAgent()
    runtime, admission = await _paused(session_id, agent)
    partial = asyncio.create_task(_deliver(runtime, _resume(session_id, is_stream=is_stream, answers=_ANSWERS[:1])))
    await asyncio.sleep(0.05)
    whole = asyncio.create_task(_deliver(runtime, _resume(session_id, is_stream=is_stream)))
    await asyncio.sleep(0.05)

    agent.gate.set()
    await asyncio.wait_for(asyncio.gather(partial, whole), 5)
    assert [[a["request_id"] for a in params["answers"]] for params in agent.delivered] == [
        ["call-a"], ["call-a", "call-b"]]
    assert await runtime.pending_interactions(session_id) == []
    assert not admission.has_pending_interaction(session_id)


@pytest.mark.asyncio
async def test_a_cancelled_pause_asks_nothing_more() -> None:
    session_id = "client-cancel"
    runtime, _admission = await _paused(session_id, _DocumentAgent())
    await runtime.cancel_request(AgentRequest(
        request_id="cancel-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel"},
    ))
    assert await runtime.pending_interactions(session_id) == []


class _MixedAgent(FakeAgent):
    """Pauses on an approval and a client tool call; a resume asks again for what it left unanswered."""

    def __init__(self) -> None:
        self.open = {"approve-1", "call-a"}
        self.delivered: list[str] = []

    def _pause(self, request: AgentRequest, ids):
        for control_id in sorted(ids):  # the approval is streamed before the client request
            event_type = "chat.client_tool_request" if control_id.startswith("call") else "chat.ask_user_question"
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                     payload={"event_type": event_type, "request_id": control_id,
                                              "source": "client_tool_interrupt" if event_type.endswith("request")
                                              else "permission_interrupt"})

    async def process_message_stream(self, request: AgentRequest):
        for chunk in self._pause(request, self.open):
            yield chunk

    async def deliver_control_input(self, request: AgentRequest):
        params = request.params
        answered = ({a["request_id"] for a in params["answers"]}
                    if params.get("source") == "client_tool_interrupt" else {params["request_id"]})
        self.delivered.append(",".join(sorted(answered)))
        self.open -= answered
        for chunk in self._pause(request, self.open):
            yield chunk
        if not self.open:
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                     payload={"event_type": "chat.final", "content": "done"}, is_complete=True)


def _user_answer(session_id: str) -> AgentRequest:
    return AgentRequest(
        request_id="answer-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        params={"query": "", "mode": "agent", "request_id": "approve-1", "source": "permission_interrupt",
                "answers": [{"card_id": "approve-1", "approved": True}]},
    )


def _client_answer(session_id: str) -> AgentRequest:
    return _resume(session_id, is_stream=False,
                   answers=[{"interrupt_id": "intr-a", "request_id": "call-a", "result": {"text": "one"}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("user_first", [True, False])
async def test_a_pause_of_an_approval_and_a_client_tool_call_resumes_in_either_order(user_first: bool) -> None:
    session_id = f"mixed-{user_first}"
    agent = _MixedAgent()
    runtime, admission = _runtime(agent)
    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "go", "mode": "agent"},
    ), trigger_hook=False))

    answers = [_user_answer, _client_answer] if user_first else [_client_answer, _user_answer]
    for answer in answers:
        await asyncio.wait_for(runtime.invoke(answer(session_id), trigger_hook=False), 5)

    assert agent.delivered == (["approve-1", "call-a"] if user_first else ["call-a", "approve-1"])
    assert not admission.has_pending_interaction(session_id)


class _ThenChatAgent(_DocumentAgent):
    """Pauses on two client calls for its first message and simply answers any later one."""

    async def process_message_stream(self, request: AgentRequest):
        if request.params.get("query") == "read both pages":
            async for chunk in super().process_message_stream(request):
                yield chunk
            return
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                 payload={"event_type": "chat.final", "content": "ok"}, is_complete=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_new_user_turn_ends_what_the_pause_it_supersedes_waits_on(streaming: bool) -> None:
    session_id = f"supersede-{streaming}"
    runtime, admission = await _paused(session_id, _ThenChatAgent())

    message = AgentRequest(request_id="turn-2", channel_id="web", session_id=session_id,
                           req_method=ReqMethod.CHAT_SEND, is_stream=streaming,
                           params={"query": "something else", "mode": "agent"})
    if streaming:
        await asyncio.wait_for(_collect_events(runtime.stream(message, trigger_hook=False)), 5)
    else:
        await asyncio.wait_for(runtime.invoke(message, trigger_hook=False), 5)

    # The agent still holds the calls, but no execution waits on them: a later bind sends nothing.
    assert await runtime.pending_interactions(session_id) == []
    assert not admission.has_pending_interaction(session_id)
    assert not admission.is_session_message_blocked(session_id)
