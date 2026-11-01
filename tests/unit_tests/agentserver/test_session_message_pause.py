# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A cross-Session message whose turn pauses on several questions waits on the whole pause.

The turn reports ``waiting_user`` once, and an answer to any question of the
pause resumes the message.
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionPersistencePolicy, SessionWorkKind
from jiuwenswarm.runtime.session.model import NoControlTargetError
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.session.session_message_service import SessionMessageService
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore
from tests.unit_tests.agentserver.test_session_messaging import _enqueue, _metadata
from tests.unit_tests.runtime.test_runtime_service import FakeAgent, FakeAgentManager, FakePlanController
from jiuwenswarm.runtime.service import AgentRuntime
from unittest.mock import AsyncMock

QUESTIONS = ("question-1", "question-2")


class _TwoQuestionRuntime:
    """Streams a turn that pauses on two questions."""

    agent_manager = object()

    def stream(self, request, **_kwargs):
        async def events():
            for question in QUESTIONS:
                yield RuntimeEvent(
                    request_id=request.request_id, channel_id=request.channel_id, session_id=request.session_id,
                    payload={"event_type": "chat.ask_user_question", "request_id": question,
                             "source": "ask_user_interrupt"},
                )

        return events()


async def _service(tmp_path) -> tuple[SessionMessageStore, SessionMessageService]:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    service = SessionMessageService(
        store=store, admission=SessionRunAdmission(), execute=lambda record: asyncio.sleep(0), available=False,
    )
    await service.start()
    return store, service


def _server(service: SessionMessageService, runtime) -> AgentWebSocketServer:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = runtime.agent_manager
    server._session_message_service = service
    return server


@pytest.mark.asyncio
async def test_a_turn_that_pauses_on_two_questions_waits_for_an_answer(tmp_path, monkeypatch) -> None:
    store, service = await _service(tmp_path)
    queued, _ = _enqueue(store, key="two-questions")
    record = store.claim(queued.message_id, "execution-1", "run-1")
    server = _server(service, _TwoQuestionRuntime())
    pushed = []

    async def send_push(message):
        pushed.append(message["payload"]["event_type"])
        return True

    server.send_push = send_push
    monkeypatch.setattr(agent_ws_server_module, "get_session_metadata", lambda *a, **k: _metadata("target-1"))
    monkeypatch.setattr(agent_ws_server_module, "build_server_push_message", lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(agent_ws_server_module, "enqueue_history_request_completion", lambda *a, **k: None)

    result = await server.execute_internal_session_message(record)

    assert result.status == "waiting_user"
    assert pushed.count("chat.ask_user_question") == 2 and "chat.error" not in pushed
    assert store.get(record.message_id).status == "waiting_user"
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", QUESTIONS)
async def test_an_answer_to_any_question_of_the_pause_resumes_the_message(tmp_path, answered: str) -> None:
    store, service = await _service(tmp_path)
    queued, _ = _enqueue(store, key=f"resume-{answered}")
    store.claim(queued.message_id, "execution-1", "run-1")
    store.mark_waiting(queued.message_id, interrupt_request_id=QUESTIONS[0], interrupt_source="ask_user_interrupt")
    coordinator = RuntimeSessionCoordinator()
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock(),
                           plan_controller=FakePlanController(), session_coordinator=coordinator)
    await coordinator.register_session("target-1", "web", SessionPersistencePolicy.PERSISTENT)
    await coordinator.run_unary(
        "target-1", f"session-message-{queued.message_id}", SessionWorkKind.SESSION_MESSAGE,
        lambda: asyncio.sleep(0), interaction_ids=lambda _: frozenset(QUESTIONS),
    )
    answer = AgentRequest(
        request_id="answer-1", channel_id="web", session_id="target-1", req_method=ReqMethod.CHAT_SEND,
        params={"query": "", "request_id": answered, "source": "ask_user_interrupt",
                "answers": [{"selected_options": ["yes"]}]},
    )

    state = await _server(service, runtime)._open_session_message_resume(answer)

    assert state is not None and state.message_id == queued.message_id
    assert answer.trusted_session_message_route["message_id"] == queued.message_id
    await service.stop()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [False, True])
async def test_an_answer_whose_pause_is_gone_settles_the_message_as_unknown(tmp_path, monkeypatch, is_stream) -> None:
    store, service = await _service(tmp_path)
    queued, _ = _enqueue(store, key=f"gone-{is_stream}")
    store.claim(queued.message_id, "execution-1", "run-1")
    store.mark_waiting(queued.message_id, interrupt_request_id="approval-1", interrupt_source="permission_interrupt")
    coordinator = RuntimeSessionCoordinator()
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock(),
                           plan_controller=FakePlanController(), session_coordinator=coordinator)
    # A restart dropped the pause: the Session is back, nothing of it waits.
    await coordinator.register_session("target-1", "web", SessionPersistencePolicy.PERSISTENT)
    server = _server(service, runtime)
    server._session_stream_tasks = {}
    server._send_runtime_event = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_ws_server_module, "enqueue_history_request_completion", lambda *a, **k: None)
    answer = AgentRequest(
        request_id="answer-1", channel_id="web", session_id="target-1", req_method=ReqMethod.CHAT_SEND,
        is_stream=is_stream, params={"query": "", "request_id": "approval-1", "source": "permission_interrupt",
                                     "answers": [{"card_id": "approval-1", "approved": True}], "mode": "agent"},
    )
    handle = server._handle_stream_impl if is_stream else server._handle_unary_impl

    with pytest.raises(NoControlTargetError):
        await asyncio.wait_for(handle(object(), answer, asyncio.Lock()), 2)

    assert store.get(queued.message_id).status == "unknown"
    await service.stop()
    await runtime.close()


class _FollowUpAgent(FakeAgent):
    def __init__(self) -> None:
        self.delivering = asyncio.Event()
        self.release = asyncio.Event()

    async def deliver_control_input(self, request):
        self.delivering.set()
        await self.release.wait()
        yield AgentResponseChunk(
            request_id=request.request_id, channel_id=request.channel_id,
            payload={"event_type": "chat.ask_user_question", "request_id": "question-3",
                     "source": "ask_user_interrupt"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_question", [QUESTIONS[1], QUESTIONS[0]])  # a sibling, the same one again
async def test_a_refused_second_answer_leaves_the_message_with_the_answer_that_resumed_it(
    tmp_path, monkeypatch, second_question,
) -> None:
    store, service = await _service(tmp_path)
    queued, _ = _enqueue(store, key="two-answers")
    store.claim(queued.message_id, "execution-1", "run-1")
    store.mark_waiting(queued.message_id, interrupt_request_id=QUESTIONS[0], interrupt_source="ask_user_interrupt")
    agent = _FollowUpAgent()
    coordinator = RuntimeSessionCoordinator()
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock(),
                           plan_controller=FakePlanController(), session_coordinator=coordinator)
    await coordinator.register_session("target-1", "web", SessionPersistencePolicy.PERSISTENT)
    turn_id = f"session-message-{queued.message_id}"

    async def mailbox_turn() -> None:
        coordinator.bind_agent("target-1", turn_id, agent)

    await coordinator.run_unary("target-1", turn_id, SessionWorkKind.SESSION_MESSAGE, mailbox_turn,
                                interaction_ids=lambda _: frozenset(QUESTIONS))
    server = _server(service, runtime)
    server._session_stream_tasks = {}
    server._send_runtime_event = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_ws_server_module, "enqueue_history_request_completion", lambda *a, **k: None)

    def answer(request_id: str, question: str) -> AgentRequest:
        return AgentRequest(
            request_id=request_id, channel_id="web", session_id="target-1", req_method=ReqMethod.CHAT_SEND,
            is_stream=True, params={"query": "", "request_id": question, "source": "ask_user_interrupt",
                                    "answers": [{"selected_options": ["yes"]}], "mode": "agent"},
        )

    first = asyncio.create_task(server._handle_stream_impl(object(), answer("answer-1", QUESTIONS[0]), asyncio.Lock()))
    await asyncio.wait_for(agent.delivering.wait(), 2)
    second = asyncio.create_task(server._handle_stream_impl(object(), answer("answer-2", second_question),
                                                            asyncio.Lock()))
    await asyncio.sleep(0.05)
    agent.release.set()
    await asyncio.wait_for(first, 2)
    with pytest.raises(NoControlTargetError):
        await asyncio.wait_for(second, 2)

    message = store.get(queued.message_id)
    assert (message.status, message.interrupt_request_id) == ("waiting_user", "question-3")
    assert coordinator.has_control_target("target-1", "question-3")
    await service.stop()
    await runtime.close()
