# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Heartbeat scheduler/host/Session Runtime chain with a deterministic agent."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat import runtime as heartbeat_module
from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HeartbeatSchedule
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import (
    SessionSummary,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.runtime.session import SessionExecutionEndedError, SessionWorkKind
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

SESSION = "existing-session"
SINGLE_AGENT_MODES = ["agent.work.normal", "agent.code.normal"]


class _Agent:
    def __init__(self, behavior):
        self.behavior = behavior
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()
        self.question_seen = asyncio.Event()
        self.answered = asyncio.Event()
        self.requests = []
        self.control_requests = []

    @staticmethod
    def _chunk(request, payload, *, complete=True):
        return AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=payload,
            metadata=request.metadata,
            is_complete=complete,
        )

    async def process_message_stream(self, request):
        self.requests.append(request)
        heartbeat = (request.metadata or {}).get("automation", {}).get(
            "kind"
        ) == "heartbeat"
        try:
            if heartbeat:
                self.entered.set()
                if self.behavior == "block":
                    await self.release.wait()
                if self.behavior == "fail":
                    raise RuntimeError("model failed")
                if self.behavior in {"ask_live", "ask_ended"}:
                    yield self._chunk(
                        request,
                        {
                            "event_type": "chat.ask_user_question",
                            "request_id": "question-1",
                        },
                        complete=self.behavior == "ask_ended",
                    )
                    if self.behavior == "ask_ended":
                        return
                    await self.answered.wait()
            yield self._chunk(request, {"event_type": "chat.final", "content": "done"})
        finally:
            if heartbeat:
                self.closed.set()

    async def deliver_control_input(self, request):
        self.control_requests.append(request)
        self.answered.set()
        yield self._chunk(request, {"event_type": "chat.final", "content": "answered"})


@pytest.fixture
def make_chain(tmp_path, monkeypatch):
    async def make(
        mode="agent.work.normal", behavior="success", timeout=10, **job_options
    ):
        agent = _Agent(behavior)
        manager = SimpleNamespace(
            begin_foreground_chat=AsyncMock(),
            end_foreground_chat=AsyncMock(),
            cleanup=AsyncMock(),
            cancel_all_inflight_work=AsyncMock(),
            cleanup_session_runtime=AsyncMock(return_value=True),
            pin_agent=Mock(),
            unpin_agent=Mock(),
            get_agent_for_session_nowait=Mock(return_value=agent),
        )
        plan = SimpleNamespace(
            ensure_state=AsyncMock(return_value=PlanStateResult()),
            check_post_process_exit=AsyncMock(return_value=[]),
            reset_session=Mock(),
        )
        runtime = AgentRuntime(
            agent_manager=manager, initializer=AsyncMock(), plan_controller=plan
        )

        async def prepare(request, channel_id, **kwargs):
            request.params["mode"] = mode
            return ("code" if ".code." in mode else "agent"), "normal", agent

        runtime.prepare_chat_turn = prepare
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._runtime = runtime
        server._agent_manager = manager

        async def push(message):
            if message["payload"].get("event_type") == "chat.ask_user_question":
                agent.question_seen.set()
            return True

        server.send_push = AsyncMock(side_effect=push)
        monkeypatch.setattr(
            heartbeat_module,
            "_load_limits",
            lambda: {"execution_timeout_seconds": timeout},
        )
        monkeypatch.setattr(
            heartbeat_module,
            "get_heartbeat_jobs_path",
            lambda: tmp_path / "heartbeat.json",
        )
        heartbeat = HeartbeatRailRuntime(server)
        heartbeat.scheduler._now_fn = lambda: 1000.0
        server._heartbeat_runtime = heartbeat
        runtime.set_admission_controller(heartbeat.admission)
        runtime.set_session_delete_lifecycle(heartbeat)
        heartbeat.scheduler._session_resolver = SimpleNamespace(
            resolve=lambda channel_id, session_id: SessionSummary(
                session_id=session_id, channel_id=channel_id
            )
        )
        schedule = job_options.pop(
            "schedule", {"type": "interval", "interval_seconds": 120}
        )
        job = await heartbeat.store.create_job(
            name="follow up",
            channel_id="web",
            session_id=SESSION,
            prompt="check progress",
            schedule=HeartbeatSchedule.from_dict(schedule),
            source="web_rpc",
            next_run_at=1000.0,
            now=999.0,
            **job_options,
        )
        return SimpleNamespace(
            runtime=runtime,
            heartbeat=heartbeat,
            server=server,
            agent=agent,
            manager=manager,
            job=job,
            mode=mode,
        )

    return make


async def _finish(chain):
    await chain.heartbeat.stop()
    await chain.runtime.close()


async def _settle(chain):
    async with asyncio.timeout(2):
        while True:
            tasks = tuple(chain.heartbeat.execution._tasks.values())
            if tasks:
                await asyncio.gather(*tasks)
            result = await chain.heartbeat.store.get_job(chain.job.id)
            if result.run_state.current_run_id is None:
                return result
            await asyncio.sleep(0)


async def _wait_heartbeat_state(chain, expected):
    async with asyncio.timeout(2):
        while True:
            snapshot = chain.runtime.session_coordinator.snapshot_session(SESSION)
            heartbeat = next(
                (
                    item
                    for item in (() if snapshot is None else snapshot.executions)
                    if item.work_kind is SessionWorkKind.HEARTBEAT
                ),
                None,
            )
            if heartbeat is not None and heartbeat.state.value == expected:
                return heartbeat
            await asyncio.sleep(0)


def _executions(chain):
    return chain.runtime.session_coordinator.snapshot_session(SESSION).executions


def _user_request(chain, **params):
    return AgentRequest(
        request_id="user-request",
        channel_id="web",
        session_id=SESSION,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": chain.mode, "content": "continue", **params},
        is_stream=True,
    )


async def _user_turn(chain, **params):
    return [
        event
        async for event in chain.runtime.stream(
            _user_request(chain, **params), trigger_hook=False
        )
    ]


@pytest.mark.parametrize(
    "mode",
    [
        *SINGLE_AGENT_MODES,
        "agent.work.plan",
        "agent.code.plan",
        "team.work.normal",
        "team.code.normal",
    ],
)
async def test_heartbeat_adopts_original_session_and_records_execution(
    make_chain, mode
):
    chain = await make_chain(mode)
    try:
        await chain.heartbeat.scheduler._tick_once()
        result = await _settle(chain)
        assert result.run_count == 1
        assert result.run_state.last_run_status == "succeeded"
        assert result.next_run_at > 1
        (execution,) = _executions(chain)
        assert execution.work_kind is SessionWorkKind.HEARTBEAT
        assert execution.state.value == "succeeded"
        (request,) = chain.agent.requests
        assert execution.request_id == request.request_id
        assert request.session_id == SESSION
        assert request.metadata["automation"]["job_id"] == chain.job.id
        chain.manager.begin_foreground_chat.assert_not_awaited()
        chain.manager.end_foreground_chat.assert_not_awaited()
        chain.manager.pin_agent.assert_called_once_with(chain.agent)
        pushes = [call.args[0] for call in chain.server.send_push.await_args_list]
        assert pushes[0]["payload"]["is_processing"] is True
        assert pushes[-1]["payload"]["is_processing"] is False
        assert all(push["session_id"] == SESSION for push in pushes)
    finally:
        await _finish(chain)


@pytest.mark.parametrize("mode", SINGLE_AGENT_MODES)
async def test_managed_user_preempts_heartbeat_without_waiting_in_its_lane(
    make_chain, mode
):
    chain = await make_chain(mode, behavior="block")
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.entered.wait(), 2)
        assert _executions(chain)[0].state.value == "running"
        events = await asyncio.wait_for(_user_turn(chain), 2)
        result = await chain.heartbeat.store.get_job(chain.job.id)
        assert result.run_state.last_run_status == "cancelled"
        assert result.run_state.last_error == "heartbeat preempted by user request"
        assert chain.agent.closed.is_set()
        assert events[-1].payload["content"] == "done"
        assert {item.work_kind: item.state.value for item in _executions(chain)} == {
            SessionWorkKind.HEARTBEAT: "cancelled",
            SessionWorkKind.CHAT_STREAM: "succeeded",
        }
        assert not chain.heartbeat.admission.is_user_active(SESSION)
        assert not chain.heartbeat.execution.active_session_ids()
    finally:
        await _finish(chain)


@pytest.mark.parametrize(
    "cancel_via", ["coordinator", "heartbeat", "session_close", "shutdown"]
)
async def test_cancel_and_close_drain_agent_and_persist_same_terminal_state(
    make_chain, cancel_via
):
    chain = await make_chain(behavior="block")
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.entered.wait(), 2)
        (execution,) = _executions(chain)
        if cancel_via == "coordinator":
            unmatched = await chain.runtime.session_coordinator.cancel_execution(
                SESSION, request_id="other"
            )
            assert unmatched.matched == 0
            assert not chain.agent.closed.is_set()
            cancelled = await chain.runtime.session_coordinator.cancel_execution(
                SESSION, request_id=execution.request_id
            )
            assert cancelled.matched == cancelled.cancelled == 1
        elif cancel_via == "heartbeat":
            assert await chain.heartbeat.execution.cancel(execution.request_id)
        elif cancel_via == "session_close":
            await chain.runtime.session_coordinator.close_session(SESSION)
        else:
            await chain.runtime.close()
        result = await chain.heartbeat.store.get_job(chain.job.id)
        assert result.run_state.last_run_status == "cancelled"
        assert _executions(chain)[0].state.value == "cancelled"
        assert chain.agent.closed.is_set()
        assert not chain.heartbeat.execution.active_session_ids()
    finally:
        await _finish(chain)


@pytest.mark.parametrize(
    "behavior,timeout,error",
    [("fail", 10, "model failed"), ("block", 0.02, "timed out")],
)
async def test_error_and_timeout_are_failed_in_both_runtime_and_store(
    make_chain, behavior, timeout, error
):
    chain = await make_chain(behavior=behavior, timeout=timeout)
    try:
        await chain.heartbeat.scheduler._tick_once()
        result = await _settle(chain)
        (execution,) = _executions(chain)
        assert result.run_state.last_run_status == execution.state.value == "failed"
        assert error in result.run_state.last_error
        assert error in execution.error
        assert chain.agent.closed.is_set()
        assert not chain.heartbeat.execution.active_session_ids()
    finally:
        await _finish(chain)


@pytest.mark.parametrize("mode", SINGLE_AGENT_MODES)
@pytest.mark.parametrize("behavior", ["ask_live", "ask_ended"])
async def test_heartbeat_interaction_answers_match_execution_and_unblock_schedule(
    make_chain, mode, behavior
):
    chain = await make_chain(mode, behavior=behavior)
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.question_seen.wait(), 2)
        if behavior == "ask_ended":
            await _wait_heartbeat_state(chain, "waiting_for_control")
        assert chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-1"
        )
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "unrelated"
        )
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert not await chain.heartbeat.admission.try_begin_heartbeat(
            SESSION, "next-run"
        )
        events = await asyncio.wait_for(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            ),
            2,
        )
        await _settle(chain)
        assert events[-1].payload["content"] == "answered"
        parent = next(
            item
            for item in _executions(chain)
            if item.work_kind is SessionWorkKind.HEARTBEAT
        )
        answer = next(
            item
            for item in _executions(chain)
            if item.work_kind is SessionWorkKind.CONTROL_INPUT
        )
        assert parent.state.value == answer.state.value == "succeeded"
        assert answer.parent_execution_id == parent.execution_id
        assert len(chain.agent.requests) == len(chain.agent.control_requests) == 1
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert await chain.heartbeat.admission.try_begin_heartbeat(SESSION, "next-run")
        await chain.heartbeat.admission.end_heartbeat(SESSION, "next-run")
    finally:
        await _finish(chain)


@pytest.mark.parametrize("mode", SINGLE_AGENT_MODES)
async def test_heartbeat_answer_blocks_another_heartbeat_until_delivery_finishes(
    make_chain, mode
):
    chain = await make_chain(mode, behavior="ask_ended")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(request):
        entered.set()
        await release.wait()
        yield chain.agent._chunk(
            request, {"event_type": "chat.final", "content": "answered"}
        )

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        answer = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert chain.heartbeat.admission.is_user_active(SESSION)
        decision = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        assert decision["accepted"] is False
        assert decision["reason"] == "session_busy"
        release.set()
        await answer
        await _settle(chain)
        assert not chain.heartbeat.admission.is_user_active(SESSION)
    finally:
        release.set()
        await _finish(chain)


@pytest.mark.parametrize("mode", SINGLE_AGENT_MODES)
async def test_heartbeat_followup_question_keeps_schedule_blocked(make_chain, mode):
    chain = await make_chain(mode, behavior="ask_ended")

    async def deliver(request):
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-2"},
        )

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        await _user_turn(
            chain,
            query="",
            request_id="question-1",
            source="ask_user_interrupt",
            answers=[{"question": "choose", "selected_options": ["A"]}],
        )
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        decision = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        assert decision["accepted"] is False
        assert decision["reason"] == "previous_run_active"
    finally:
        async def complete(request):
            yield chain.agent._chunk(
                request, {"event_type": "chat.final", "content": "answered"}
            )

        chain.agent.deliver_control_input = complete
        if chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        ):
            await _user_turn(
                chain,
                query="",
                request_id="question-2",
                source="ask_user_interrupt",
                answers=[{"question": "continue", "selected_options": ["yes"]}],
            )
            await _settle(chain)
        await _finish(chain)


@pytest.mark.parametrize("mode", SINGLE_AGENT_MODES)
async def test_live_heartbeat_keeps_new_question_emitted_during_answer(
    make_chain, mode
):
    chain = await make_chain(mode, behavior="ask_live")
    question_two_consumed = asyncio.Event()
    release_heartbeat = asyncio.Event()

    async def heartbeat_stream(request):
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-1"},
        )
        await chain.agent.answered.wait()
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-2"},
        )
        question_two_consumed.set()
        await release_heartbeat.wait()

    async def deliver(request):
        chain.agent.answered.set()
        if request.params["request_id"] == "question-1":
            await question_two_consumed.wait()
        yield chain.agent._chunk(
            request, {"event_type": "chat.final", "content": "answered"}
        )

    chain.agent.process_message_stream = heartbeat_stream
    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.question_seen.wait(), 2)
        await _user_turn(
            chain,
            query="",
            request_id="question-1",
            source="ask_user_interrupt",
            answers=[{"question": "choose", "selected_options": ["A"]}],
        )
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        running = await chain.heartbeat.store.get_job(chain.job.id)
        assert running.run_state.current_run_id is not None
        assert running.run_state.last_run_status is None
        assert running.run_count == 0
        assert chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        heartbeat = next(
            item
            for item in _executions(chain)
            if item.work_kind is SessionWorkKind.HEARTBEAT
        )
        assert heartbeat.state.value == "running"
        assert heartbeat.waiting_control_id == "question-2"
        assert not await chain.heartbeat.admission.try_begin_heartbeat(
            SESSION, "next-run"
        )

        release_heartbeat.set()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        assert chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        await _user_turn(
            chain,
            query="",
            request_id="question-2",
            source="ask_user_interrupt",
            answers=[{"question": "continue", "selected_options": ["yes"]}],
        )
        await _settle(chain)
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        assert await chain.heartbeat.admission.try_begin_heartbeat(
            SESSION, "next-run"
        )
        await chain.heartbeat.admission.end_heartbeat(SESSION, "next-run")
    finally:
        release_heartbeat.set()
        await _finish(chain)


@pytest.mark.parametrize("mode", SINGLE_AGENT_MODES)
async def test_live_heartbeat_parent_finishes_before_answer_delivery(
    make_chain, mode
):
    chain = await make_chain(mode, behavior="ask_live")

    async def deliver(request):
        chain.agent.answered.set()
        while True:
            heartbeat = next(
                item
                for item in _executions(chain)
                if item.work_kind is SessionWorkKind.HEARTBEAT
            )
            if heartbeat.state.value == "waiting_for_control":
                break
            await asyncio.sleep(0)
        yield chain.agent._chunk(
            request, {"event_type": "chat.final", "content": "answered"}
        )

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.question_seen.wait(), 2)
        await _user_turn(
            chain,
            query="",
            request_id="question-1",
            source="ask_user_interrupt",
            answers=[{"question": "choose", "selected_options": ["A"]}],
        )
        await _settle(chain)
        executions = _executions(chain)
        assert [item.state.value for item in executions] == [
            "succeeded",
            "succeeded",
        ]
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-1"
        )
        assert chain.runtime.session_coordinator.snapshot_session(SESSION).state.value == (
            "ready"
        )
        assert await chain.heartbeat.admission.try_begin_heartbeat(
            SESSION, "next-run"
        )
        await chain.heartbeat.admission.end_heartbeat(SESSION, "next-run")
    finally:
        await _finish(chain)


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_failed_heartbeat_answer_restores_interaction_protection(
    make_chain, outcome
):
    chain = await make_chain(behavior="ask_ended")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(_request):
        entered.set()
        if outcome == "cancelled":
            await release.wait()
        raise RuntimeError("answer failed")
        yield  # pragma: no cover

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        answer = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        if outcome == "cancelled":
            answer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await answer
        else:
            with pytest.raises(RuntimeError, match="answer failed"):
                await answer
        assert not chain.heartbeat.admission.is_user_active(SESSION)
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-1"
        )
        assert not await chain.heartbeat.admission.try_begin_heartbeat(
            SESSION, "next-run"
        )
    finally:
        release.set()
        await _finish(chain)


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_followup_question_is_not_retained_when_answer_fails(
    make_chain, outcome
):
    chain = await make_chain(behavior="ask_ended")
    question_two_emitted = asyncio.Event()
    release = asyncio.Event()

    async def deliver(request):
        question_two_emitted.set()
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-2"},
        )
        if outcome == "cancelled":
            await release.wait()
        raise RuntimeError("answer failed")

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        answer = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(question_two_emitted.wait(), 2)
        if outcome == "cancelled":
            answer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await answer
        else:
            with pytest.raises(RuntimeError, match="answer failed"):
                await answer

        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-1"
        )
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        heartbeat = await _wait_heartbeat_state(chain, "waiting_for_control")
        assert heartbeat.waiting_control_id == "question-1"

        async def complete(request):
            yield chain.agent._chunk(
                request, {"event_type": "chat.final", "content": "answered"}
            )

        chain.agent.deliver_control_input = complete
        await _user_turn(
            chain,
            query="",
            request_id="question-1",
            source="ask_user_interrupt",
            answers=[{"question": "retry", "selected_options": ["A"]}],
        )
        result = await _settle(chain)
        assert result.run_state.last_run_status == "succeeded"
    finally:
        release.set()
        await _finish(chain)


async def test_cancelling_heartbeat_while_answer_runs_cancels_control_child(
    make_chain,
):
    chain = await make_chain(behavior="ask_ended")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(request):
        entered.set()
        await release.wait()
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-2"},
        )

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        answer = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        running = await chain.heartbeat.store.get_job(chain.job.id)
        assert await chain.heartbeat.execution.cancel(
            running.run_state.current_run_id
        )
        with pytest.raises(asyncio.CancelledError):
            await answer

        executions = _executions(chain)
        assert {item.state.value for item in executions} == {"cancelled"}
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-1"
        )
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        result = await chain.heartbeat.store.get_job(chain.job.id)
        assert result.run_state.last_run_status == "cancelled"
    finally:
        release.set()
        await _finish(chain)


async def test_failing_heartbeat_while_answer_runs_cancels_control_child(make_chain):
    chain = await make_chain(behavior="ask_live")
    answer_entered = asyncio.Event()
    fail_parent = asyncio.Event()
    release = asyncio.Event()

    async def heartbeat_stream(request):
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-1"},
        )
        await fail_parent.wait()
        raise RuntimeError("parent failed")

    async def deliver(_request):
        answer_entered.set()
        await release.wait()
        yield  # pragma: no cover

    chain.agent.process_message_stream = heartbeat_stream
    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.question_seen.wait(), 2)
        answer = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(answer_entered.wait(), 2)
        fail_parent.set()
        with pytest.raises(asyncio.CancelledError):
            await answer
        result = await _settle(chain)

        executions = _executions(chain)
        parent = next(
            item
            for item in executions
            if item.work_kind is SessionWorkKind.HEARTBEAT
        )
        child = next(
            item
            for item in executions
            if item.work_kind is SessionWorkKind.CONTROL_INPUT
        )
        assert parent.state.value == "failed"
        assert child.state.value == "cancelled"
        assert result.run_state.last_run_status == "failed"
        assert "parent failed" in result.run_state.last_error
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
    finally:
        release.set()
        await _finish(chain)


async def test_late_control_result_from_closed_generation_is_discarded(make_chain):
    chain = await make_chain(behavior="ask_ended")
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    chain.runtime.session_coordinator._cancel_timeout = 0.01

    async def deliver(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-2"},
        )

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        answer = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        await chain.heartbeat.begin_session_delete(SESSION)
        await asyncio.wait_for(cancelled.wait(), 2)
        await chain.runtime.cleanup_session(
            channel_id="web", session_id=SESSION, reset_plan_state=False
        )
        await chain.heartbeat.abort_session_delete(SESSION, channel_id="web")
        release.set()
        with pytest.raises(SessionExecutionEndedError) as excinfo:
            await answer
        # A void answer is a failure the client must hear about. Reporting it as
        # a cancellation would let upstream stream handlers drop it silently.
        assert not isinstance(excinfo.value, asyncio.CancelledError)

        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
        assert not chain.runtime.session_coordinator.has_control_target(
            SESSION, "question-2"
        )
        chain.agent.behavior = "success"
        assert (await chain.heartbeat.scheduler.trigger_run_now(chain.job.id))[
            "accepted"
        ]
        result = await _settle(chain)
        assert result.run_state.last_run_status == "succeeded"
    finally:
        release.set()
        await _finish(chain)


async def test_concurrent_answers_claim_question_once(make_chain):
    chain = await make_chain(behavior="ask_ended")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def deliver(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        yield chain.agent._chunk(
            request, {"event_type": "chat.final", "content": "answered"}
        )

    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await _wait_heartbeat_state(chain, "waiting_for_control")
        first = asyncio.create_task(
            _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["A"]}],
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(RuntimeError, match="already being delivered"):
            await _user_turn(
                chain,
                query="",
                request_id="question-1",
                source="ask_user_interrupt",
                answers=[{"question": "choose", "selected_options": ["B"]}],
            )
        release.set()
        await first
        result = await _settle(chain)
        assert calls == 1
        assert result.run_state.last_run_status == "succeeded"
        assert not chain.heartbeat.admission.has_pending_interaction(SESSION)
    finally:
        release.set()
        await _finish(chain)


@pytest.mark.parametrize("policy", ["skip", "queue", "replace"])
async def test_manual_concurrency_policy_preserves_exact_execution_identity(
    make_chain, policy
):
    chain = await make_chain(behavior="block", concurrency_policy=policy)
    try:
        first = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        await asyncio.wait_for(chain.agent.entered.wait(), 2)
        if policy == "replace":
            chain.agent.behavior = "success"
        second = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        if policy == "skip":
            assert second["accepted"] is False
            assert second["reason"] == "previous_run_active"
        else:
            assert second["accepted"] is True
        if policy != "replace":
            chain.agent.behavior = "success"
            chain.agent.release.set()
        # Finishing a queued run can dispatch another task; drain both rounds.
        for _ in range(3):
            await _settle(chain)
        executions = {item.request_id: item for item in _executions(chain)}
        assert first["run_id"] in executions
        if policy == "skip":
            assert len(executions) == 1
        else:
            assert len(executions) == 2
            assert executions[second["run_id"]].state.value == "succeeded"
        assert executions[first["run_id"]].state.value == (
            "cancelled" if policy == "replace" else "succeeded"
        )
    finally:
        await _finish(chain)


@pytest.mark.parametrize(
    "schedule",
    [
        {"type": "interval", "interval_seconds": 120},
        {"type": "cron", "cron_expr": "*/5 * * * *"},
        {"type": "once", "run_at": 1000.0},
    ],
)
async def test_schedule_completion_retains_runtime_result_and_stops_future_runs(
    make_chain, schedule
):
    chain = await make_chain(schedule=schedule, max_runs=1)
    try:
        await chain.heartbeat.scheduler._tick_once()
        result = await _settle(chain)
        assert result.status == "completed"
        assert result.enabled is False
        assert result.run_count == 1
        assert result.next_run_at is None
        denied = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        assert denied["reason"] == "job_completed"
        assert len(_executions(chain)) == 1
    finally:
        await _finish(chain)


async def test_session_delete_quiesces_managed_run_and_abort_allows_new_generation(
    make_chain,
):
    chain = await make_chain(behavior="block")
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.entered.wait(), 2)
        (previous,) = _executions(chain)
        await chain.heartbeat.begin_session_delete(SESSION)
        assert not await chain.heartbeat.should_retain_session(SESSION)
        await chain.runtime.cleanup_session(
            channel_id="web", session_id=SESSION, reset_plan_state=False
        )
        denied = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        assert denied["reason"] == "session_deleting"
        await chain.heartbeat.abort_session_delete(SESSION, channel_id="web")
        chain.agent.behavior = "success"
        assert (await chain.heartbeat.scheduler.trigger_run_now(chain.job.id))[
            "accepted"
        ]
        await _settle(chain)
        (current,) = _executions(chain)
        assert current.generation == previous.generation + 1
        assert current.state.value == "succeeded"
        assert (
            chain.runtime.session_coordinator.get_execution(
                previous.execution_id
            ).state.value
            == "cancelled"
        )
    finally:
        await _finish(chain)


async def test_cancel_before_runtime_entry_releases_claim_without_running_agent(
    make_chain,
):
    chain = await make_chain()
    try:
        started = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        assert await chain.heartbeat.execution.cancel(started["run_id"])
        assert not chain.agent.entered.is_set()
        assert not chain.heartbeat.execution.active_session_ids()
        result = await chain.heartbeat.store.get_job(chain.job.id)
        assert result.run_state.last_run_status == "cancelled"
    finally:
        await _finish(chain)


async def test_cold_runtime_startup_remains_inside_heartbeat_deadline(make_chain):
    chain = await make_chain(timeout=0.02)
    startup_entered = asyncio.Event()

    async def initialize():
        startup_entered.set()
        await asyncio.Event().wait()

    chain.runtime._initializer = initialize
    try:
        await chain.heartbeat.scheduler._tick_once()
        result = await _settle(chain)
        (execution,) = _executions(chain)
        assert startup_entered.is_set()
        assert not chain.agent.entered.is_set()
        assert execution.state.value == result.run_state.last_run_status == "failed"
        assert "timed out" in execution.error
        assert not chain.heartbeat.execution.active_session_ids()
    finally:
        await _finish(chain)


async def test_scheduler_background_loop_dispatches_managed_once_job(make_chain):
    chain = await make_chain(schedule={"type": "once", "run_at": 1000.0})
    finished = asyncio.Event()

    async def on_completed(session_id):
        await chain.heartbeat._release_if_no_active_jobs(session_id)
        finished.set()

    chain.heartbeat.execution.set_completion_hook(on_completed)
    try:
        await chain.heartbeat.start()
        assert chain.heartbeat.is_available
        await asyncio.wait_for(finished.wait(), 2)
        assert chain.agent.entered.is_set()
        result = await chain.heartbeat.store.get_job(chain.job.id)
        assert result.status == "completed"
        assert _executions(chain)[0].state.value == "succeeded"
    finally:
        await _finish(chain)


async def test_runtime_owner_is_resolved_again_after_host_replacement(make_chain):
    chain = await make_chain()
    try:
        old_runtime = chain.runtime
        await old_runtime.close()
        replacement = AgentRuntime(
            agent_manager=chain.manager,
            initializer=AsyncMock(),
            plan_controller=old_runtime.plan_controller,
        )
        replacement.prepare_chat_turn = old_runtime.prepare_chat_turn
        replacement.set_admission_controller(chain.heartbeat.admission)
        chain.server._runtime = replacement
        chain.runtime = replacement
        await chain.heartbeat.scheduler._tick_once()
        result = await _settle(chain)
        assert result.run_state.last_run_status == "succeeded"
        assert old_runtime.session_coordinator.snapshot_session(SESSION) is None
        assert len(_executions(chain)) == 1
    finally:
        await _finish(chain)


async def test_busy_foreground_defers_heartbeat_without_creating_execution(make_chain):
    chain = await make_chain()
    try:
        await chain.heartbeat.admission.begin_user(SESSION)
        decision = await chain.heartbeat.scheduler.trigger_run_now(chain.job.id)
        assert decision["reason"] == "session_busy"
        assert chain.runtime.session_coordinator.snapshot_session(SESSION) is None
        await chain.heartbeat.admission.end_user(SESSION)
        assert (await chain.heartbeat.scheduler.trigger_run_now(chain.job.id))[
            "accepted"
        ]
        assert (await _settle(chain)).run_state.last_run_status == "succeeded"
    finally:
        await _finish(chain)


async def test_heartbeat_does_not_serialize_active_goal_execution(make_chain):
    chain = await make_chain()
    await chain.runtime.register_session(session_id=SESSION, channel_id="web")
    goal_entered = asyncio.Event()
    goal_release = asyncio.Event()

    async def goal():
        goal_entered.set()
        await goal_release.wait()

    task = asyncio.create_task(
        chain.runtime.session_coordinator.run_unary(
            SESSION,
            "goal-1",
            SessionWorkKind.GOAL_STREAM,
            goal,
        )
    )
    try:
        await asyncio.wait_for(goal_entered.wait(), 2)
        await chain.heartbeat.scheduler._tick_once()
        assert (await _settle(chain)).run_state.last_run_status == "succeeded"
        assert not task.done()
        states = {item.work_kind: item.state.value for item in _executions(chain)}
        assert states == {
            SessionWorkKind.GOAL_STREAM: "running",
            SessionWorkKind.HEARTBEAT: "succeeded",
        }
    finally:
        goal_release.set()
        await asyncio.wait_for(task, 2)
        await _finish(chain)


async def test_question_after_followup_parks_heartbeat_is_dropped_loudly(
    make_chain, caplog
):
    """A question nobody owns must be logged, and must not steal the slot."""
    chain = await make_chain(behavior="ask_live")
    third_emitted = asyncio.Event()
    release_heartbeat = asyncio.Event()

    async def heartbeat_stream(request):
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-1"},
            complete=False,
        )
        await chain.agent.answered.wait()
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-3"},
        )
        third_emitted.set()
        await release_heartbeat.wait()

    async def deliver(request):
        yield chain.agent._chunk(
            request,
            {"event_type": "chat.ask_user_question", "request_id": "question-2"},
        )

    chain.agent.process_message_stream = heartbeat_stream
    chain.agent.deliver_control_input = deliver
    try:
        await chain.heartbeat.scheduler._tick_once()
        await asyncio.wait_for(chain.agent.question_seen.wait(), 2)
        await _user_turn(
            chain,
            query="",
            request_id="question-1",
            source="ask_user_interrupt",
            answers=[{"question": "choose", "selected_options": ["A"]}],
        )
        # The follow-up answer parked the Heartbeat in waiting_for_control, so
        # no RUNNING execution is left to own a further question.
        heartbeat = await _wait_heartbeat_state(chain, "waiting_for_control")
        assert heartbeat.waiting_control_id == "question-2"

        with caplog.at_level(logging.WARNING, logger="jiuwenswarm.runtime.service"):
            chain.agent.answered.set()
            await asyncio.wait_for(third_emitted.wait(), 2)

        assert "dropping heartbeat interaction" in caplog.text
        assert "question-3" in caplog.text
        # The dropped question must not overwrite the slot the user still owes.
        parked = next(
            item
            for item in _executions(chain)
            if item.work_kind is SessionWorkKind.HEARTBEAT
        )
        assert parked.waiting_control_id == "question-2"
    finally:
        release_heartbeat.set()
        await _finish(chain)
