# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import gc
import weakref
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from tests.unit_tests.agentserver.test_deep_adapter_session_teardown import _IdleChildAdapter, _make_adapter
from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController, _collect_events


@pytest.fixture(autouse=True)
def isolated_agent_dirs(tmp_path, monkeypatch):
    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions)


QUESTION = {"event_type": "chat.ask_user_question", "request_id": "q-1", "source": "ask_user_interrupt",
            "questions": [{"question": "go?"}]}


class _Manager(FakeAgentManager):
    get_agent_for_session_nowait = AgentManager.get_agent_for_session_nowait
    borrow_agent_nowait = AgentManager.borrow_agent_nowait
    _borrow_agent = AgentManager._borrow_agent
    _release_agent_borrower = AgentManager._release_agent_borrower

    def __init__(self, agent: JiuWenSwarm) -> None:
        super().__init__()
        self.agent = agent
        self.agents = {"web": {"agent:normal:": agent}}
        self._agent_borrowers = {}

    def _schedule_pending_tui_retirement(self, _agent_id):
        return None


def _web_agent(session_id: str, delivered: list[dict]) -> tuple[JiuWenSwarm, object]:
    router = _make_adapter(
        _is_session_scoped_adapter=False, _session_adapters={}, _session_adapter_locks={},
        _session_adapter_last_used={}, _session_adapter_versions={}, _session_adapter_reload_failures={},
        _active_session_ids={}, _session_agent_tasks={}, SESSION_ADAPTER_EVICT_BATCH_SIZE=8,
        SESSION_ADAPTER_IDLE_TTL_SEC=1.0,
    )
    agent = object.__new__(JiuWenSwarm)
    agent._session_manager = SessionManager()
    agent._adapter = router

    async def process_message_stream(request):
        router._session_adapters[session_id] = _IdleChildAdapter()
        router._session_adapter_last_used[session_id] = 1.0
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id, payload=QUESTION)

    async def deliver_control_input(request):
        delivered.append(request.params)
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                 payload={"event_type": "chat.final", "content": "ok"}, is_complete=True)

    agent.process_message_stream = process_message_stream
    agent.deliver_control_input = deliver_control_input
    return agent, router


async def _paused_then_evicted(session_id: str, delivered: list[dict]) -> tuple[AgentRuntime, _Manager]:
    agent, router = _web_agent(session_id, delivered)
    manager = _Manager(agent)
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=FakePlanController(),
                           admission_controller=SessionRunAdmission())
    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-1", channel_id="web", session_id=session_id, req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "go", "mode": "agent"}), trigger_hook=False))
    await router._evict_idle_session_adapters()
    assert manager.get_agent_for_session_nowait("web", session_id) is None
    return runtime, manager


def _answer(session_id: str, *, streamed: bool) -> AgentRequest:
    return AgentRequest(request_id="resume-1", channel_id="web", session_id=session_id,
                        req_method=ReqMethod.CHAT_SEND, is_stream=streamed,
                        params={"source": "ask_user_interrupt", "request_id": "q-1",
                                "answers": [{"selected_options": ["yes"]}], "mode": "agent"})


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_an_answer_reaches_a_paused_turn_whose_session_was_evicted(streamed: bool) -> None:
    delivered: list[dict] = []
    runtime, _manager = await _paused_then_evicted(f"web-{streamed}", delivered)

    answer = _answer(f"web-{streamed}", streamed=streamed)
    if streamed:
        events = await asyncio.wait_for(_collect_events(runtime.stream(answer, trigger_hook=False)), 5)
    else:
        events = await asyncio.wait_for(runtime.invoke(answer, trigger_hook=False), 5)

    assert [params["request_id"] for params in delivered] == ["q-1"]
    assert any(event.payload.get("event_type") == "chat.final" for event in events)


@pytest.mark.asyncio
async def test_an_answer_is_refused_once_its_turns_agent_was_recreated() -> None:
    delivered: list[dict] = []
    runtime, manager = await _paused_then_evicted("web-recreated", delivered)
    manager.agents = {"web": {"agent:normal:": object.__new__(JiuWenSwarm)}}

    with pytest.raises(RuntimeError, match="session has no active agent"):
        await asyncio.wait_for(_collect_events(runtime.stream(_answer("web-recreated", streamed=True),
                                                              trigger_hook=False)), 5)
    assert delivered == []


@pytest.mark.asyncio
async def test_a_superseded_pause_lets_its_recreated_agent_go() -> None:
    delivered: list[dict] = []
    runtime, manager = await _paused_then_evicted("web-superseded", delivered)
    retired = weakref.ref(manager.agent)
    replacement, _router = _web_agent("web-superseded", delivered)
    manager.agent = replacement
    manager.agents = {"web": {"agent:normal:": replacement}}

    await _collect_events(runtime.stream(AgentRequest(
        request_id="turn-2", channel_id="web", session_id="web-superseded", req_method=ReqMethod.CHAT_SEND,
        is_stream=True, params={"query": "again", "mode": "agent"}), trigger_hook=False))
    gc.collect()

    assert retired() is None

