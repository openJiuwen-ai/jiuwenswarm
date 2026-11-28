# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rejected Team answers remain recoverable through Runtime entry points."""

import asyncio
from contextlib import aclosing
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.interaction import interaction_ids
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session.model import (
    SessionExecutionState,
    SessionWorkKind,
    TeamExecutionDetails,
    TeamWaiterKind,
)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: {"mode": "team.work", "work_mode": "work"},
    )
    value = AgentRuntime(agent_manager=Mock(), initializer=AsyncMock())
    value._started = True
    monkeypatch.setattr(value, "_team_lifecycle", Mock())
    monkeypatch.setattr(value, "_remove_team_response_waiter", AsyncMock())
    return value


def event(event_type, *, ok=True, complete=False):
    chunk = AgentResponseChunk(
        request_id="answer", channel_id="web",
        payload={"event_type": event_type} if event_type else None,
        is_complete=complete,
    )
    result = RuntimeEvent.from_agent_message(
        chunk, request_id="answer", channel_id="web", session_id="team",
    )
    result.ok = ok
    return result


def answer_request(request_id="answer"):
    return AgentRequest(
        request_id=request_id, channel_id="web", session_id="team",
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={
            "mode": "team.work", "query": "", "request_id": "question",
            "source": "ask_user_interrupt",
            "answers": [{"selected_options": ["continue"]}],
        },
    )


async def start_question(runtime, *, parked):
    coordinator = runtime._session_coordinator
    await coordinator.register_session("team", "web")
    finish = asyncio.Event()

    async def original(_context):
        question = event("chat.ask_user_question")
        question.payload["request_id"] = "question"
        yield question
        await finish.wait()

    stream = coordinator.run_team_stream(
        "team", "owner", SessionWorkKind.TEAM_REQUEST, original,
        details=TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT),
        interaction_ids=interaction_ids,
    )
    await anext(stream)
    if parked:
        coordinator.mark_session_waiting_for_input("team", 1)
    return stream, finish


def execution(runtime, request_id):
    return next(
        item for item in runtime.get_session_snapshot("team").executions
        if item.request_id == request_id
    )


def set_response(monkeypatch, runtime, events, *, raises=False, hold=None):
    async def stream_started(request, **kwargs):
        del request, kwargs
        for item in events:
            yield item
        if hold is not None:
            await hold.wait()
        if raises:
            raise RuntimeError("connection failed after delivery")

    async def invoke_started(request, **kwargs):
        del request, kwargs
        return events

    monkeypatch.setattr(runtime, "_stream_started", stream_started)
    monkeypatch.setattr(runtime, "_invoke_started", invoke_started)


async def collect(runtime, entrypoint, request):
    if entrypoint == "invoke":
        request.is_stream = False
        return await runtime.invoke(request, trigger_hook=False)
    async with aclosing(runtime.stream(request, trigger_hook=False)) as stream:
        return [item async for item in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["stream", "invoke"])
@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("failure_type", [
    "chat.error", "runtime.error", "execution.error", "team.error", "error",
    "team.interact.failed", "failed-with-ok-false",
])
async def test_rejected_answer_remains_recoverable_and_can_retry(
    monkeypatch, runtime, entrypoint, parked, failure_type,
):
    stream, finish = await start_question(runtime, parked=parked)
    coordinator = runtime._session_coordinator
    if failure_type == "runtime.error":
        failure = RuntimeEvent.error(
            request_id="answer", channel_id="web", session_id="team",
            error=RuntimeError("delivery rejected"),
        )
    else:
        failure = event(failure_type, ok=failure_type != "failed-with-ok-false")
    output = [failure, event(None, complete=True)]
    set_response(monkeypatch, runtime, output)
    try:
        assert await collect(runtime, entrypoint, answer_request()) == output
        assert execution(runtime, "owner").waiting_control_ids == frozenset({"question"})
        assert execution(runtime, "answer").state is SessionExecutionState.FAILED
        assert coordinator.recoverable_control("team", "question", execution(runtime, "owner").execution_id)

        set_response(monkeypatch, runtime, [event("chat.processing_status_deferred"), event(None, complete=True)])
        await collect(runtime, entrypoint, answer_request("retry"))
        assert not coordinator.unanswered_interactions("team")
        assert execution(runtime, "retry").state is SessionExecutionState.SUCCEEDED
        finish.set()
        async for _ in stream:
            pass
        assert execution(runtime, "owner").state is SessionExecutionState.SUCCEEDED
    finally:
        finish.set()
        await stream.aclose()
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["stream", "invoke"])
@pytest.mark.parametrize("receipt", ["team.input.accepted", "chat.processing_status_deferred"])
async def test_accepted_answer_is_not_reopened_by_later_business_error(
    monkeypatch, runtime, entrypoint, receipt,
):
    stream, finish = await start_question(runtime, parked=True)
    set_response(monkeypatch, runtime, [event(receipt), event("chat.error"), event(None, complete=True)])
    try:
        await collect(runtime, entrypoint, answer_request())
        assert not runtime._session_coordinator.unanswered_interactions("team")
        finish.set()
        async for _ in stream:
            pass
        assert execution(runtime, "owner").state is SessionExecutionState.SUCCEEDED
    finally:
        finish.set()
        await stream.aclose()
        await runtime._session_coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_stream_acceptance_retires_question_before_tail_finishes(monkeypatch, runtime, raises):
    stream, finish = await start_question(runtime, parked=True)
    tail = asyncio.Event()
    set_response(monkeypatch, runtime, [event("team.input.accepted")], raises=raises, hold=tail)
    answer = runtime.stream(answer_request(), trigger_hook=False)
    try:
        assert (await anext(answer)).event_type == "team.input.accepted"
        assert not runtime._session_coordinator.unanswered_interactions("team")
        if raises:
            tail.set()
            with pytest.raises(RuntimeError, match="after delivery"):
                await anext(answer)
        else:
            await answer.aclose()
        assert not runtime._session_coordinator.unanswered_interactions("team")
    finally:
        finish.set()
        await answer.aclose()
        await stream.aclose()
        await runtime._session_coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_first", [False, True])
async def test_overlapping_rejected_answer_does_not_remove_followup_question(monkeypatch, runtime, reject_first):
    stream, finish = await start_question(runtime, parked=True)
    entered = {name: asyncio.Event() for name in ["accepted", "rejected"]}
    release = {name: asyncio.Event() for name in entered}
    next_question = event("chat.ask_user_question")
    next_question.payload["request_id"] = "next-question"

    async def answer_stream(request, **kwargs):
        del kwargs
        entered[request.request_id].set()
        await release[request.request_id].wait()
        if request.request_id == "accepted":
            yield event("team.input.accepted")
            yield next_question
        else:
            yield event("chat.error")
        yield event(None, complete=True)

    monkeypatch.setattr(runtime, "_stream_started", answer_stream)
    tasks = {
        name: asyncio.create_task(collect(runtime, "stream", answer_request(name)))
        for name in entered
    }
    try:
        await asyncio.gather(*(ready.wait() for ready in entered.values()))
        first = "rejected" if reject_first else "accepted"
        second = "accepted" if reject_first else "rejected"
        release[first].set()
        await tasks[first]
        release[second].set()
        await tasks[second]
        coordinator = runtime._session_coordinator
        assert coordinator.unanswered_interactions("team") == frozenset({"next-question"})
        assert coordinator.has_control_target("team", "next-question")
        assert execution(runtime, "accepted").state is SessionExecutionState.WAITING_FOR_CONTROL
        assert execution(runtime, "rejected").state is SessionExecutionState.FAILED
    finally:
        for gate in release.values():
            gate.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        finish.set()
        await stream.aclose()
        await runtime._session_coordinator.close()


@pytest.mark.asyncio
async def test_answer_receipt_does_not_revive_cancelled_owner(monkeypatch, runtime):
    stream, finish = await start_question(runtime, parked=True)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def answer_stream(request, **kwargs):
        del request, kwargs
        entered.set()
        await release.wait()
        yield event("team.input.accepted")

    monkeypatch.setattr(runtime, "_stream_started", answer_stream)
    task = asyncio.create_task(collect(runtime, "stream", answer_request()))
    coordinator = runtime._session_coordinator
    try:
        await entered.wait()
        await coordinator.cancel_execution("team", execution_id=execution(runtime, "owner").execution_id)
        release.set()
        await task
        assert execution(runtime, "owner").state is SessionExecutionState.CANCELLED
        assert not coordinator.unanswered_interactions("team")
        assert not coordinator.has_control_target("team", "question")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        finish.set()
        await stream.aclose()
        await coordinator.close()
