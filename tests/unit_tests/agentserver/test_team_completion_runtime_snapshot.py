# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team completion must agree with the Worker's recovery snapshot."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session.model import (
    SessionExecutionHandle,
    SessionExecutionState,
    SessionWorkKind,
    TeamExecutionDetails,
    TeamWaiterKind,
)
from jiuwenswarm.server.ipc.protocol import dump_agent_request
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


def team_request(request_id="team-input"):
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        user_id="owner",
        session_id="team-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team", "query": "hello"},
        is_stream=True,
    )


def team_execution(request_id, state, **kwargs):
    return SessionExecutionHandle(
        execution_id="team-execution",
        session_id="team-session",
        request_id=request_id,
        generation=1,
        work_kind=SessionWorkKind.TEAM_REQUEST,
        team=TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT),
        state=SessionExecutionState(state),
        **kwargs,
    ).snapshot()


async def recovery_snapshot(dispatcher, request):
    conn = SimpleNamespace(send=AsyncMock())
    generation = dispatcher._bridge.holder.open_session(conn)
    await dispatcher._subscribe_locked(
        {"payload": dump_agent_request(request)}, conn, generation,
    )
    return conn.send.call_args.args[0]["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_active", [True, False])
@pytest.mark.parametrize(
    "state", ["queued", "running", "waiting_for_control", "waiting_for_input", "succeeded", "failed", "cancelled"],
)
async def test_team_recovery_uses_execution_activity_instead_of_handler_lifetime(state, handler_active):
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    record = bridge.events.register(request, "team-dispatch")
    record.is_processing = True
    execution = team_execution(request.request_id, state)
    record.last_execution_id = execution.execution_id
    if not handler_active:
        bridge.events.finish(record.dispatch_id)
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(executions=(execution,)),
    )

    snapshot = await recovery_snapshot(dispatcher, request)

    assert snapshot["is_processing"] is (state in {"queued", "running", "waiting_for_control"})
    assert record.active is handler_active


@pytest.mark.asyncio
async def test_completed_team_submission_clears_busy_after_execution_is_no_longer_retained():
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    record = bridge.events.register(request, "submitted-input")
    record.is_processing = True
    bridge.events.append(
        encode_agent_chunk_for_wire(
            AgentResponseChunk(
                request_id=request.request_id,
                channel_id="web",
                payload={"content": "", "execution_id": "finished-input-execution"},
                is_complete=True,
            ),
            response_id=request.request_id,
            sequence=1,
        ),
        record.dispatch_id,
    )
    bridge.events.finish(record.dispatch_id)
    observer = replace(team_request("team-observer"), params={"mode": "team", "attach_only": True})
    observer_record = bridge.events.register(observer, "persistent-observer")
    bridge.events.append(
        {"body": {"event_type": "chat.processing_status", "is_processing": False, "is_complete": True}},
        observer_record.dispatch_id,
    )
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(executions=()),
    )

    snapshot = await recovery_snapshot(dispatcher, request)

    assert snapshot["is_processing"] is False
    assert record.is_processing is False
    assert observer_record.active is True
    assert len(bridge.events.replay(request, after=0)) == 2
    bridge.events.register(request, "replacement-input")
    assert (await recovery_snapshot(dispatcher, request))["is_processing"] is True


@pytest.mark.asyncio
async def test_returned_output_segment_stays_busy_while_its_control_is_waiting():
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    record = bridge.events.register(request, "waiting-control")
    record.last_execution_id = "team-execution"
    record.interactions["approval"] = {"control_id": "approval"}
    bridge.events.finish(record.dispatch_id)
    execution = team_execution(request.request_id, "waiting_for_control", waiting_control_id="approval")
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(executions=(execution,)),
    )

    snapshot = await recovery_snapshot(dispatcher, request)

    assert snapshot["is_processing"] is True
    assert snapshot["dispatches"][0]["pending_interactions"] == [{"control_id": "approval"}]


@pytest.mark.asyncio
async def test_child_execution_keeps_returned_parent_busy_after_parent_execution_is_evicted():
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    record = bridge.events.register(request, "parent-dispatch")
    record.last_execution_id = "evicted-parent-execution"
    bridge.events.finish(record.dispatch_id)
    child = team_execution("child-request", "running", root_request_id=request.request_id)
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(executions=(child,)),
    )

    assert (await recovery_snapshot(dispatcher, request))["is_processing"] is True
    assert record.is_processing is True


@pytest.mark.asyncio
async def test_missing_runtime_snapshot_preserves_unresolved_execution_state():
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    record = bridge.events.register(request, "unresolved-dispatch")
    record.is_processing = True
    bridge.events.finish(record.dispatch_id)
    dispatcher._runtime = SimpleNamespace(get_session_snapshot=lambda _: None)

    assert (await recovery_snapshot(dispatcher, request))["is_processing"] is True
    assert record.is_processing is True


@pytest.mark.asyncio
async def test_persistent_team_owner_can_resume_after_idle_recovery():
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    record = bridge.events.register(request, "persistent-user-owner")
    handle = SessionExecutionHandle(
        execution_id="persistent-execution",
        session_id=request.session_id,
        request_id=request.request_id,
        generation=1,
        work_kind=SessionWorkKind.TEAM_REQUEST,
        team=TeamExecutionDetails(waiter_kind=TeamWaiterKind.PERSISTENT),
        state=SessionExecutionState.WAITING_FOR_INPUT,
    )
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(executions=(handle.snapshot(),)),
    )

    assert (await recovery_snapshot(dispatcher, request))["is_processing"] is False
    handle.state = SessionExecutionState.RUNNING
    assert (await recovery_snapshot(dispatcher, request))["is_processing"] is True
    handle.state = SessionExecutionState.WAITING_FOR_INPUT
    assert (await recovery_snapshot(dispatcher, request))["is_processing"] is False
    assert record.active is True


@pytest.mark.asyncio
async def test_team_recovery_does_not_clear_another_callers_execution_state():
    bridge = WorkerGatewayBridge(ConnHolder())
    dispatcher = WorkerDispatcher(bridge)
    request = team_request()
    other = replace(request, user_id="another-owner")
    own_record = bridge.events.register(request, "own-input")
    other_record = bridge.events.register(other, "other-input")
    for record in (own_record, other_record):
        record.is_processing = True
        bridge.events.finish(record.dispatch_id)
    dispatcher._runtime = SimpleNamespace(
        get_session_snapshot=lambda _: SimpleNamespace(executions=()),
    )

    snapshot = await recovery_snapshot(dispatcher, request)

    assert snapshot["is_processing"] is False
    assert other_record.is_processing is True
    assert [item["dispatch_id"] for item in snapshot["dispatches"]] == [own_record.dispatch_id]
