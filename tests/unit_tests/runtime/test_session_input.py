# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""New Session inputs must neither wait for nor resume the current task."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.service import AgentRuntime, RuntimeStateError
from jiuwenswarm.runtime.session.model import SessionExecutionState, SessionWorkKind


def request(rid="supplement", *, input_mode="steer", **params):
    return AgentRequest(
        request_id=rid, channel_id="web", session_id="input-session",
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"query": rid, "mode": "agent", **({"input_mode": input_mode} if input_mode else {}), **params},
    )


async def collect(stream):
    return [event async for event in stream]


class GatedAgent:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.delivered = []
        self.cancelled = False
        self.fail_input = False
        self.hold_input = False
        self.input_entered = asyncio.Event()
        self.input_cancelled = asyncio.Event()
        self.question = False

    async def execute_message(self, req):
        async for chunk in self.process_message_stream(req):
            if chunk.is_complete:
                return AgentResponse(request_id=req.request_id, channel_id=req.channel_id, payload=chunk.payload)

    async def process_message_stream(self, req):
        self.entered.set()
        if self.question:
            yield AgentResponseChunk(
                request_id=req.request_id, channel_id=req.channel_id,
                payload={"event_type": "chat.ask_user_question", "request_id": "question-1"},
            )
            return
        try:
            await self.release.wait()
            yield AgentResponseChunk(
                request_id=req.request_id, channel_id=req.channel_id,
                payload={"event_type": "chat.final", "content": "original completed"}, is_complete=True,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def deliver_session_input(self, req):
        assert get_current_runtime() is not None
        self.input_entered.set()
        if self.fail_input:
            raise ValueError("input rejected")
        if self.hold_input:
            try:
                await asyncio.Event().wait()
            finally:
                self.input_cancelled.set()
        self.delivered.append(req)
        yield AgentResponseChunk(
            request_id=req.request_id, channel_id=req.channel_id,
            payload={"event_type": "runtime.accepted", "request_id": req.request_id},
        )


@pytest.fixture
async def setup_runtime(monkeypatch):
    agent = GatedAgent()
    manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(), cleanup=AsyncMock(),
        begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock(),
        get_agent_for_session_nowait=Mock(return_value=agent),
    )
    plan = SimpleNamespace(
        active_sessions=set(),
        ensure_state=AsyncMock(return_value=SimpleNamespace(events=[])),
        check_post_process_exit=AsyncMock(return_value=[]), reset_session=Mock(),
    )
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=plan)
    monkeypatch.setattr(runtime, "_prepare_chat_turn", AsyncMock(return_value=("agent", None, agent)))
    monkeypatch.setattr(runtime, "_trigger_before_chat_request_hook", AsyncMock())
    await runtime._register_session(session_id="input-session", channel_id="web")
    yield runtime, agent, manager
    agent.release.set()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
@pytest.mark.parametrize("mode", ["steer", "follow_up", "alias"])
async def test_new_input_bypasses_busy_lane_without_answer_or_second_agent(setup_runtime, unary, mode):
    runtime, agent, manager = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    supplement = (request(input_mode=None, runtime_mode=" STEER ")
                  if mode == "alias" else request(input_mode=mode))
    # The originating channel differs from the cached owner's channel.
    supplement.channel_id = "internal"
    supplement.is_stream = not unary
    events = await asyncio.wait_for(
        runtime.invoke(supplement) if unary else collect(runtime.stream(supplement)), 2,
    )
    assert [e.event_type for e in events] == ["runtime.accepted"]
    assert events[0].request_id == "supplement"
    assert events[0].channel_id == "internal"
    assert not original.done() and not agent.cancelled
    assert [r.request_id for r in agent.delivered] == ["supplement"]
    manager.get_agent_for_session_nowait.assert_called_once_with("web", "input-session")
    runtime._prepare_chat_turn.assert_awaited_once()
    snapshot = runtime._session_coordinator.snapshot_session("input-session")
    root = next(e for e in snapshot.executions if e.request_id == "original")
    child = next(e for e in snapshot.executions if e.request_id == "supplement")
    assert root.state is SessionExecutionState.RUNNING
    assert child.parent_execution_id == root.execution_id
    assert child.work_kind is SessionWorkKind.SESSION_INPUT
    agent.release.set()
    assert (await asyncio.wait_for(original, 2))[-1].payload["content"] == "original completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_mode", ["plan", "goal", "agent.code.plan", "agent.work.plan"])
@pytest.mark.parametrize("busy", [False, True])
async def test_protected_cross_session_input_never_uses_active_or_idle_delivery(
    setup_runtime, protected_mode, busy,
):
    from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
    from jiuwenswarm.runtime.session_input import SessionInputQueueRequiredError

    runtime, agent, manager = setup_runtime
    original = None
    if busy:
        original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
        await asyncio.wait_for(agent.entered.wait(), 2)
    if protected_mode == "plan":
        runtime.plan_controller.active_sessions.add("input-session")
    elif protected_mode == "goal":
        manager.has_active_goal = Mock(return_value=True)
    supplement = request(**{SESSION_MESSAGE_INTERNAL_KEY: {"message_id": "sm-steer"}})
    if protected_mode.startswith("agent."):
        supplement.params["mode"] = protected_mode
    with pytest.raises(SessionInputQueueRequiredError):
        await collect(runtime.stream(supplement))
    assert not agent.delivered
    assert not agent.cancelled
    if original is not None:
        assert not original.done()
        agent.release.set()
        await asyncio.wait_for(original, 2)
    else:
        assert not agent.entered.is_set()


@pytest.mark.asyncio
async def test_input_rejection_does_not_end_original(setup_runtime):
    runtime, agent, _ = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    agent.fail_input = True
    with pytest.raises(ValueError, match="input rejected"):
        await collect(runtime.stream(request()))
    assert not original.done() and not agent.cancelled
    agent.release.set()
    await original


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
async def test_idle_input_owns_normal_execution_and_output(setup_runtime, unary):
    runtime, agent, manager = setup_runtime
    agent.release.set()
    req = request()
    req.is_stream = not unary
    events = await runtime.invoke(req) if unary else await collect(runtime.stream(req))
    if not unary:
        assert events[0].event_type == "runtime.accepted"
        assert events[0].payload["input_delivery"] == "chat"
        assert events[0].request_id == req.request_id
        assert events[0].payload["execution_id"] == events[-1].payload["execution_id"]
    assert events[-1].event_type == "chat.final"
    manager.get_agent_for_session_nowait.assert_not_called()
    runtime._prepare_chat_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_input_does_not_answer_or_supersede_pending_interaction(setup_runtime):
    runtime, agent, _ = setup_runtime
    agent.question = True
    await collect(runtime.stream(request("original", input_mode=None)))
    with pytest.raises(RuntimeError, match="waiting for an interaction answer"):
        await collect(runtime.stream(request()))
    root = runtime._session_coordinator.snapshot_session("input-session").executions[0]
    assert root.state is SessionExecutionState.WAITING_FOR_CONTROL
    assert root.waiting_control_id == "question-1"
    assert not agent.delivered


@pytest.mark.asyncio
async def test_close_cancels_inflight_input_and_cannot_reopen_session(setup_runtime):
    runtime, agent, _ = setup_runtime
    agent.hold_input = True
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    supplement = asyncio.create_task(collect(runtime.stream(request())))
    await asyncio.wait_for(agent.input_entered.wait(), 2)
    await runtime._session_coordinator.close_session("input-session")
    results = await asyncio.gather(original, supplement, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert agent.input_cancelled.is_set()
    with pytest.raises(RuntimeStateError, match="closed"):
        await collect(runtime.stream(request("late")))


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"query": " "}, {"images": ["image.png"]}, {"mode": "team"}])
async def test_unsupported_inputs_fail_without_dispatch(setup_runtime, params):
    runtime, agent, _ = setup_runtime
    with pytest.raises(ValueError):
        await collect(runtime.stream(request(**params)))
    assert not agent.delivered
    runtime._prepare_chat_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_unowned_input_does_not_create_another_agent(setup_runtime):
    runtime, agent, _ = setup_runtime
    supplement = request()
    supplement.session_id = "other-runtime-session"
    with pytest.raises(RuntimeStateError, match="not owned"):
        await collect(runtime.stream(supplement))
    runtime._prepare_chat_turn.assert_not_awaited()
    assert not agent.delivered


@pytest.mark.asyncio
async def test_conflicting_modes_fail_without_dispatch(setup_runtime):
    runtime, _, _ = setup_runtime
    with pytest.raises(ValueError, match="must agree"):
        await collect(runtime.stream(request(runtime_mode="follow_up")))
    runtime._prepare_chat_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
async def test_bound_input_keeps_original_execution_and_returns_its_id(setup_runtime, unary):
    runtime, agent, _ = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    before = runtime._session_coordinator.snapshot_session("input-session")
    target = before.executions[0].execution_id
    req = request(expected_execution_id=target)
    req.is_stream = not unary
    events = await runtime.invoke(req) if unary else await collect(runtime.stream(req))
    assert [event.event_type for event in events] == ["runtime.accepted"]
    assert events[0].payload["execution_id"] == target
    after = runtime._session_coordinator.snapshot_session("input-session")
    assert len([item for item in after.executions if item.work_kind is SessionWorkKind.CHAT_STREAM]) == 1
    assert not original.done() and not agent.cancelled
    agent.release.set()
    assert (await original)[-1].payload["execution_id"] == target


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["idle", "ended", "different_execution"])
async def test_bound_input_never_falls_back_or_targets_replacement(setup_runtime, state):
    runtime, agent, _ = setup_runtime
    target = "missing-execution"
    if state == "ended":
        agent.release.set()
        events = await collect(runtime.stream(request("original", input_mode=None)))
        target = events[-1].payload["execution_id"]
    original = None
    if state == "different_execution":
        original = asyncio.create_task(collect(runtime.stream(request("replacement", input_mode=None))))
        await asyncio.wait_for(agent.entered.wait(), 2)
    before = runtime._session_coordinator.snapshot_session("input-session")
    with pytest.raises(RuntimeError, match="targeted execution has ended or changed"):
        await collect(runtime.stream(request(expected_execution_id=target)))
    after = runtime._session_coordinator.snapshot_session("input-session")
    assert after.executions == before.executions
    assert not agent.delivered and not agent.cancelled
    if original:
        assert not original.done()
        agent.release.set()
        await original


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"expected_execution_id": ""}, {"expected_execution_id": 42},
    {"expected_execution_id": "original", "input_mode": "follow_up"},
])
async def test_invalid_execution_binding_is_rejected(setup_runtime, params):
    runtime, agent, _ = setup_runtime
    with pytest.raises(ValueError, match="expected_execution_id"):
        await collect(runtime.stream(request(**params)))
    assert not agent.delivered
    runtime._prepare_chat_turn.assert_not_awaited()
