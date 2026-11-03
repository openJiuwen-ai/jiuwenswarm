# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron runtime state crosses channels while recovery details stay private."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRef, AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session.model import (
    SessionExecutionHandle,
    SessionExecutionState,
    SessionWorkKind,
)
from jiuwenswarm.server.front import router
from jiuwenswarm.server.ipc.protocol import dump_agent_request
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


@pytest.mark.asyncio
@pytest.mark.parametrize("passive", [False, True])
@pytest.mark.parametrize(
    "state, handler_active, processing",
    [
        ("warming", True, True),
        ("admitted", True, True),
        ("queued", True, True),
        ("running", True, True),
        ("running", False, True),
        ("waiting_for_control", False, True),
        ("waiting_for_input", True, False),
        ("waiting_for_input", False, False),
        ("succeeded", True, False),
        ("succeeded", False, False),
        ("failed", True, False),
        ("cancelled", False, False),
        ("evicted", False, False),
    ],
)
async def test_web_observes_cron_activity_without_receiving_its_replay(state, handler_active, processing, passive):
    connection = SimpleNamespace(send=AsyncMock())
    holder = ConnHolder()
    generation = holder.open_session(connection)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    work = AgentRequest(
        request_id="cron-run",
        channel_id="__cron__",
        user_id="owner",
        session_id="cron-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"attach_only": passive},
        is_stream=True,
    )
    record = bridge.events.register(work, "cron-dispatch")
    bridge.events.append(
        {"request_id": work.request_id, "body": {"event_type": "chat.delta", "content": "private cron output"}},
        record.dispatch_id,
    )
    record.is_processing = True
    if not handler_active:
        bridge.events.finish(record.dispatch_id)
    if state != "warming":
        executions = ()
        if state not in {"admitted", "evicted"}:
            executions = (
                SessionExecutionHandle(
                    execution_id="cron-execution",
                    session_id=work.session_id,
                    request_id=work.request_id,
                    root_request_id=work.request_id,
                    generation=1,
                    state=SessionExecutionState(state),
                    work_kind=SessionWorkKind.TEAM_REQUEST,
                ).snapshot(),
            )
        dispatcher._runtime = SimpleNamespace(
            get_session_snapshot=lambda _: SimpleNamespace(generation=1, executions=executions),
        )
    query = replace(work, request_id="web-metadata", channel_id="web", req_method=ReqMethod.SESSION_GET_METADATA)

    await dispatcher._subscribe_locked(
        {"payload": dump_agent_request(query), "subscribe": True}, connection, generation,
    )

    snapshot = connection.send.call_args.args[0]["payload"]
    assert snapshot["is_processing"] is (processing and not passive)
    assert snapshot["dispatches"] == []
    assert snapshot.get("executions", []) == []
    assert connection.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["user", "app", "agent", "session"])
async def test_cross_channel_query_preserves_other_session_owners(identity):
    connection = SimpleNamespace(send=AsyncMock())
    holder = ConnHolder()
    generation = holder.open_session(connection)
    bridge = WorkerGatewayBridge(holder)
    dispatcher = WorkerDispatcher(bridge)
    work = AgentRequest(
        request_id="cron-run", channel_id="__cron__", user_id="owner", session_id="cron-session",
        req_method=ReqMethod.CHAT_SEND, params={},
    )
    record = bridge.events.register(work, "cron-dispatch")
    record.is_processing = True
    bridge.events.finish(record.dispatch_id)
    dispatcher._runtime = SimpleNamespace(get_session_snapshot=lambda _: SimpleNamespace(executions=()))
    query = replace(work, request_id="web-metadata", channel_id="web", req_method=ReqMethod.SESSION_GET_METADATA)
    if identity == "user":
        query = replace(query, user_id="another-owner")
    elif identity == "app":
        query = replace(query, metadata={"app_id": "another-app"})
    elif identity == "agent":
        query = replace(query, agent_ref=AgentRef(mode="agent", id="another-agent"))
    else:
        query = replace(query, session_id="another-session")

    await dispatcher._subscribe_locked({"payload": dump_agent_request(query)}, connection, generation)

    snapshot = connection.send.call_args.args[0]["payload"]
    assert snapshot["is_processing"] is False
    assert snapshot["dispatches"] == []
    assert record.is_processing is True


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_stopped", [False, True])
@pytest.mark.parametrize("persisted_processing", [False, True])
@pytest.mark.parametrize("processing", [False, True, None, "missing"])
async def test_metadata_status_tracks_known_runtime_state_and_preserves_pending_recovery(
    monkeypatch, processing, persisted_processing, owner_stopped,
):
    persisted_status = "processing" if persisted_processing else "idle"

    async def persisted_metadata(request):
        return AgentResponse(
            request_id=request.request_id, channel_id="web",
            payload={"status": persisted_status, "is_processing": persisted_processing,
                     "organization_owner_stopped": owner_stopped},
        )

    monkeypatch.setattr(router, "_HANDLE_SESSION", persisted_metadata)
    snapshot = {"recovery_pending": True} if processing == "missing" else {"is_processing": processing}
    backend = SimpleNamespace(get_runtime_snapshot=AsyncMock(return_value=snapshot))
    method_router = router.MethodRouter(SimpleNamespace(), SimpleNamespace(backend=backend))
    method_router._send = AsyncMock()
    query = AgentRequest(
        request_id="metadata", channel_id="web", req_method=ReqMethod.SESSION_GET_METADATA,
        params={"session_id": "cron-session"},
    )

    await method_router._dispatch_control(None, query, None)

    result = method_router._send.call_args.args[2]["body"]["result"]
    expected_processing = False if owner_stopped else (
        processing if isinstance(processing, bool) else persisted_processing
    )
    assert result["runtime"] == snapshot
    assert result["is_processing"] is expected_processing
    assert result["status"] == ("processing" if expected_processing else "idle")


@pytest.mark.parametrize("module_name", ["jiuwenswarm.runtime.cron.models", "gateway.runtime.cron.models"])
def test_cron_timeout_default_remains_one_hour_with_explicit_ten_minute_override(module_name):
    from importlib import import_module

    models = import_module(module_name)
    normal = SimpleNamespace(mode="agent.work.normal", timeout_seconds=None)
    team = SimpleNamespace(mode="team.work.normal", timeout_seconds=None)
    explicit = SimpleNamespace(mode="agent.work.normal", timeout_seconds=600)
    assert models.resolve_cron_job_timeout_seconds(normal) == 3600
    assert models.resolve_cron_job_timeout_seconds(team) == 3600
    assert models.resolve_cron_job_timeout_seconds(explicit) == 600
