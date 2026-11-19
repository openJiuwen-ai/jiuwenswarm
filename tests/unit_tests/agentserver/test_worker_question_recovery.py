# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recover only actionable questions through the real Runtime/Worker boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
from jiuwenswarm.runtime.session.execution_registry import SessionExecutionRegistry
from jiuwenswarm.runtime.session.model import SessionWorkKind, TeamExecutionDetails, TeamWaiterKind
from jiuwenswarm.server.ipc.protocol import dump_agent_request
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge

pytestmark = pytest.mark.asyncio


def request(rid="root", **params):
    return AgentRequest(request_id=rid, channel_id="web", session_id="warm", user_id="owner",
                        req_method=ReqMethod.CHAT_SEND, is_stream=True, params={"session_id": "warm", **params})


def question(control_id, source="permission_interrupt", **extra):
    return {"event_type": "chat.ask_user_question", "request_id": control_id, "source": source,
            "questions": [{"question": control_id}], **extra}


class Connection:
    def __init__(self):
        self.frames = []
        self.after_snapshot = None

    async def send(self, frame):
        self.frames.append(frame)
        if frame.get("kind") == "execution.snapshot" and self.after_snapshot is not None:
            await self.after_snapshot()


class Recovery:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.runtime = AgentRuntime.__new__(AgentRuntime)
        self.runtime._session_coordinator = coordinator
        self.runtime._admission_controller = None
        self.runtime._mark_pending_interaction = AsyncMock()
        self.runtime._mark_pending_interaction_id = AsyncMock()
        self.conn = Connection()
        self.holder = ConnHolder()
        self.generation = self.holder.open_session(self.conn)
        self.bridge = WorkerGatewayBridge(self.holder)
        self.dispatcher = WorkerDispatcher(self.bridge)
        self.dispatcher._runtime = self.runtime

    def append(self, dispatch_id, body):
        record = self.bridge.events.records[dispatch_id]
        return self.bridge.events.append({"request_id": record.request.request_id, "is_stream": True,
                                          "body": body}, dispatch_id)

    async def root(self, ids, rid="root", source="permission_interrupt"):
        dispatch_id = "dispatch-" + rid
        self.bridge.events.register(request(rid), dispatch_id)
        before = {x.execution_id for x in self.coordinator.snapshot_session("warm").executions}

        async def operation():
            for control_id in ids:
                yield control_id

        values = [v async for v in self.coordinator.run_stream(
            "warm", rid, SessionWorkKind.CHAT_STREAM, operation, interaction_ids=lambda value: frozenset({value}))]
        execution = next(x for x in self.coordinator.snapshot_session("warm").executions
                         if x.execution_id not in before)
        for value in values:
            self.append(dispatch_id, question(value, source, execution_id=execution.execution_id))
        self.bridge.events.finish(dispatch_id)
        return dispatch_id, execution.execution_id

    async def answer(self, control_id, following=(), *, unary=False, dispatch_id=None, source="permission_interrupt"):
        dispatch_id = dispatch_id or "answer-" + control_id
        accepted = request("transport-" + dispatch_id, request_id=control_id, source="ask_user_interrupt")
        self.bridge.events.register(accepted, dispatch_id)

        async def operation(_request, **_kwargs):
            for control in following:
                yield RuntimeEvent(accepted.request_id, "web", "warm", question(control, source))
            yield RuntimeEvent(accepted.request_id, "web", "warm", {"event_type": "chat.final"})

        if unary:
            async def collect(_request, **_kwargs):
                return [event async for event in operation(_request)]
            self.runtime._deliver_control_started = collect
            events = await self.runtime._deliver_control(accepted)
        else:
            self.runtime._stream_control_started = operation
            events = [event async for event in self.runtime._stream_question_answer(accepted)]
        for event in events:
            self.append(dispatch_id, event.payload)
        self.bridge.events.finish(dispatch_id)
        return dispatch_id, events

    async def subscribe(self, *, after=0, owner="owner"):
        self.conn.frames.clear()
        query = replace(request("metadata"), req_method=ReqMethod.SESSION_GET_METADATA, is_stream=False, user_id=owner)
        await self.dispatcher._subscribe({"payload": dump_agent_request(query), "subscribe": True,
                                          "after": after}, self.conn, self.generation)
        snapshot = next(frame["payload"] for frame in self.conn.frames if frame.get("kind") == "execution.snapshot")
        replay = [frame for frame in self.conn.frames if frame.get("replayed")]
        bodies = [frame["payload"].get("body") for frame in replay]
        questions = [body for body in bodies if isinstance(body, dict)
                     and body.get("event_type") == "chat.ask_user_question"]
        return snapshot, questions, replay


@pytest.fixture
async def recovery():
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session("warm", "web")
    try:
        yield Recovery(coordinator)
    finally:
        await coordinator.close()


@pytest.mark.parametrize("unary", [False, True])
@pytest.mark.parametrize("source", ["permission_interrupt", "ask_user_interrupt", "confirm_interrupt"])
async def test_completed_questions_never_replay_but_current_continuation_does(recovery, unary, source):
    await recovery.root(["A"], source=source)
    _, questions, _ = await recovery.subscribe()
    assert [q["request_id"] for q in questions] == ["A"]
    dispatch_id, events = await recovery.answer("A", ["B"], unary=unary, source=source)
    child = next(x for x in recovery.coordinator.snapshot_session("warm").executions if x.request_id == "A")
    assert all(event.payload["execution_id"] == child.execution_id for event in events)
    assert recovery.bridge.events.records[dispatch_id].last_execution_id == child.execution_id
    snapshot, questions, _ = await recovery.subscribe()
    assert [q["request_id"] for q in questions] == ["B"]
    assert not [q for d in snapshot["dispatches"] for q in d["pending_interactions"]]
    await recovery.answer("B", unary=unary)
    snapshot, questions, replay = await recovery.subscribe()
    assert questions == []
    assert all(x.state.terminal for x in recovery.coordinator.snapshot_session("warm").executions)
    assert len(replay) == 2  # Both final outputs still recover.
    assert all(frame["payload"]["metadata"]["runtime_event"]["replayed"] for frame in replay)


@pytest.mark.parametrize("first,second", [("A", "B"), ("B", "A")])
async def test_all_pending_ids_recover_and_keep_existing_handoff(recovery, first, second):
    await recovery.root(["A", "B"])
    _, questions, _ = await recovery.subscribe()
    assert [q["request_id"] for q in questions] == ["A", "B"]
    await recovery.answer(first, [second])
    _, questions, _ = await recovery.subscribe()
    assert [q["request_id"] for q in questions] == [second]
    await recovery.answer(second)
    assert (await recovery.subscribe())[1] == []


async def test_latest_question_version_and_incremental_cursor(recovery):
    dispatch, execution = await recovery.root(["A"])
    latest = recovery.append(dispatch, question("A", execution_id=execution, questions=[{"question": "latest"}]))
    _, questions, _ = await recovery.subscribe()
    assert len(questions) == 1 and questions[0]["questions"] == [{"question": "latest"}]
    assert (await recovery.subscribe(after=latest["sequence"]))[1] == []


async def test_reused_control_and_transport_ids_do_not_revive_old_round(recovery):
    old_dispatch, old_execution = await recovery.root(["A"])
    await recovery.answer("A", ["B"], dispatch_id="old-answer")
    await recovery.answer("B")
    new_dispatch, new_execution = await recovery.root(["A"], rid="new")
    # A terminal transport request id may also be used by another round.
    old_request_id = recovery.bridge.events.records[old_dispatch].request.request_id
    recovery.bridge.events.records[new_dispatch].request.request_id = old_request_id
    await recovery.answer("A", ["B"], dispatch_id="new-answer")
    snapshot, questions, replay = await recovery.subscribe()
    assert len(questions) == 1 and questions[0]["request_id"] == "B"
    assert questions[0]["execution_id"] not in {old_execution, new_execution}
    assert [f["dispatch_id"] for f in replay if f["payload"]["body"]["event_type"] == "chat.ask_user_question"] == ["new-answer"]


async def test_control_completing_during_snapshot_send_is_not_replayed(recovery):
    await recovery.root(["A"])
    recovery.conn.after_snapshot = lambda: recovery.answer("A")
    _, questions, _ = await recovery.subscribe()
    assert questions == []


async def test_recovery_requires_authority_and_the_same_owner_scope(recovery):
    dispatch, execution = await recovery.root(["A"])
    recovery.append(dispatch, question("A"))  # Fall back to the exact dispatch owner.
    recovery.append(dispatch, {"event_type": "chat.delta", "content": "retained"})
    _, questions, _ = await recovery.subscribe()
    assert len(questions) == 1 and "execution_id" not in questions[0]
    assert recovery.runtime.recoverable_control("warm", "A", execution) is not None
    assert (await recovery.subscribe(owner="another-user"))[1] == []
    recovery.dispatcher._runtime = None
    snapshot, questions, replay = await recovery.subscribe()
    assert snapshot["recovery_pending"] is True and questions == []
    assert replay[0]["payload"]["body"]["content"] == "retained"
    recovery.dispatcher._runtime = recovery.runtime
    assert len((await recovery.subscribe())[1]) == 1


async def test_pruned_terminal_parent_does_not_revive_old_card_or_hide_current_child():
    coordinator = RuntimeSessionCoordinator(registry=SessionExecutionRegistry(terminal_capacity=0))
    await coordinator.register_session("warm", "web")
    recovery = Recovery(coordinator)
    try:
        _, parent = await recovery.root(["A"])
        await recovery.answer("A", ["B"])
        assert coordinator.get_execution(parent) is None
        _, questions, _ = await recovery.subscribe()
        assert [q["request_id"] for q in questions] == ["B"]
    finally:
        await coordinator.close()


async def test_recovery_query_preserves_claims_when_callback_fails(recovery):
    _, execution = await recovery.root(["A"])

    def fail(_snapshot):
        raise ValueError("observer failed")

    with pytest.raises(ValueError, match="observer failed"):
        await recovery.coordinator.deliver_control("warm", ("A",), AsyncMock(), on_execution=fail)
    assert recovery.runtime.recoverable_control("warm", "A", execution) is not None


@pytest.mark.parametrize("retained", [0, 1])
async def test_evicted_question_frames_keep_all_pending_snapshot_fallbacks(recovery, retained):
    recovery.bridge.events._max_events = retained
    await recovery.root(["A", "B"])
    snapshot, questions, _ = await recovery.subscribe()
    assert snapshot["history_required"]
    pending = [q["request_id"] for d in snapshot["dispatches"] for q in d["pending_interactions"]]
    assert set(pending) == {"A", "B"}
    assert {q["request_id"] for q in questions} == ({"B"} if retained else set())


@pytest.mark.parametrize("retained", [0, 2048])
async def test_claim_failure_keeps_question_snapshot_retry(recovery, retained):
    recovery.bridge.events._max_events = retained
    dispatch, execution = await recovery.root(["A", "B"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def reject():
        entered.set()
        await release.wait()
        raise ValueError("preparation rejected")

    task = asyncio.create_task(recovery.coordinator.deliver_control("warm", ("A",), reject))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        snapshot, questions, _ = await recovery.subscribe()
        assert not questions
        assert snapshot["history_required"] is (retained == 0)
        assert recovery.coordinator.has_control_target("warm", "A")
        assert all(not d["pending_interactions"] for d in snapshot["dispatches"])
        assert set(recovery.bridge.events.records[dispatch].interactions) == {"A", "B"}
    finally:
        release.set()
        with pytest.raises(ValueError, match="preparation rejected"):
            await task
    assert recovery.runtime.recoverable_control("warm", "A", execution) is not None
    snapshot, _, _ = await recovery.subscribe()
    assert {q["request_id"] for d in snapshot["dispatches"] for q in d["pending_interactions"]} == {"A", "B"}


@pytest.mark.parametrize("fault", [None, "failure", "cancel"])
@pytest.mark.parametrize("unary", [False, True])
async def test_persistent_team_recovery_does_not_replay_an_answered_question(recovery, fault, unary):
    answered, finish = asyncio.Event(), asyncio.Event()
    details = TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT)
    dispatch = "team-producer"
    recovery.bridge.events.register(request("team-root", mode="team"), dispatch)

    async def producer(_context):
        yield "A"
        yield "C"
        await answered.wait()
        yield "B"
        await finish.wait()

    root = recovery.coordinator.run_team_stream(
        "warm", "team-root", SessionWorkKind.TEAM_REQUEST, producer,
        details=details, interaction_ids=lambda value: frozenset({value}),
    )
    try:
        assert await asyncio.wait_for(anext(root), 2) == "A"
        execution = next(x for x in recovery.coordinator.snapshot_session("warm").executions
                         if x.request_id == "team-root")
        recovery.append(dispatch, question("A", execution_id=execution.execution_id))
        assert await asyncio.wait_for(anext(root), 2) == "C"
        recovery.append(dispatch, question("C", execution_id=execution.execution_id))
        async def deliver(rid, *, reject=False):
            async def operation():
                if reject:
                    raise asyncio.CancelledError() if fault == "cancel" else ValueError("rejected")
                answered.set()
                return "accepted"

            if unary:
                return await recovery.coordinator.run_unary(
                    "warm", rid, SessionWorkKind.TEAM_CONTROL, operation,
                    team=TeamExecutionDetails(control_id="A"),
                )

            async def stream(_context):
                yield await operation()

            return [item async for item in recovery.coordinator.run_team_stream(
                "warm", rid, SessionWorkKind.TEAM_CONTROL, stream,
                details=TeamExecutionDetails(control_id="A"),
            )]

        if fault:
            with pytest.raises(asyncio.CancelledError if fault == "cancel" else ValueError):
                await deliver("failed-team-answer", reject=True)
            assert recovery.runtime.recoverable_control("warm", "A", execution.execution_id) is not None
        assert recovery.runtime.recoverable_control("warm", "C", execution.execution_id) is not None
        assert await deliver("team-answer") == ("accepted" if unary else ["accepted"])
        assert await asyncio.wait_for(anext(root), 2) == "B"
        recovery.append(dispatch, question("B", execution_id=execution.execution_id))
        assert recovery.runtime.recoverable_control("warm", "A", execution.execution_id) is None
        snapshot, questions, _ = await recovery.subscribe()
        assert [q["request_id"] for q in questions] == ["C", "B"]
        assert {q["request_id"] for d in snapshot["dispatches"]
                for q in d["pending_interactions"]} <= {"C", "B"}
    finally:
        finish.set()
        await root.aclose()
