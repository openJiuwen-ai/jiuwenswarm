# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Migration boundary contracts for ``session.create``.

These tests intentionally keep transport behavior separate from the Runtime
implementation.  The Runtime double records the domain order that a prepared
create guarantees; the Server is responsible only for input adaptation, wire
delivery, and selecting commit versus abort at the delivery boundary.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import (
    SessionCreateInput,
    SessionCreateResult,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionState,
)
from jiuwenswarm.server.agent_ws_server import AdapterRegistry, AgentWebSocketServer


class RecordingWebSocket:
    """Record wire delivery while optionally failing selected sends."""

    def __init__(
        self,
        trace: list[str],
        *,
        send_outcomes: list[BaseException | None] | None = None,
    ) -> None:
        self.trace = trace
        self.send_outcomes = list(send_outcomes or [])
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        self.trace.append("response.send")
        outcome = self.send_outcomes.pop(0) if self.send_outcomes else None
        if outcome is not None:
            raise outcome
        self.sent.append(json.loads(payload))


class CreateRuntime:
    """Runtime-boundary double with an observable prepared resource."""

    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.agent_manager = object()
        self.inputs: list[SessionCreateInput] = []
        self.contexts: list[SessionProvisionCommitContext] = []
        self.prepared: list[Any] = []
        self.abort_calls: list[Any] = []
        self.commit_calls: list[Any] = []
        self.commit_entered = asyncio.Event()
        self.resource_released = False

    async def start(self) -> None:
        self.trace.append("runtime.start")

    async def prepare_session_create(
        self,
        provision_input: SessionCreateInput,
    ) -> Any:
        self.inputs.append(provision_input)
        # A successful Runtime prepare must preserve this domain order.
        self.trace.extend(("create.claim", "create.metadata", "create.team"))
        prepared = SimpleNamespace(
            state=SessionProvisionState.PREPARED,
            result=SessionCreateResult(
                channel_id=provision_input.channel_id,
                session_id="created-session",
                project_id="resolved-project",
                project_dir="/resolved/project",
                work_mode="work",
                persist_session=provision_input.persist_session,
                prewarm_hit=True,
                prewarm_status="ready",
                created=True,
                # The Server-visible canonical value remains the legacy
                # ``agent`` wire form; metadata persistence performs its own
                # lazy migration to ``agent.work.normal``.
                canonical_mode="agent",
            ),
        )
        self.prepared.append(prepared)
        return prepared

    async def commit_session_provision(
        self,
        prepared: Any,
        *,
        timing: SessionProvisionCommitTiming,
        context: SessionProvisionCommitContext,
    ) -> SessionCreateResult:
        assert timing is SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY
        self.commit_calls.append(prepared)
        self.contexts.append(context)
        self.trace.append("create.kvc")
        prepared.state = SessionProvisionState.COMMITTED
        self.commit_entered.set()
        return prepared.result

    async def abort_session_provision(self, prepared: Any) -> None:
        self.trace.append("create.abort")
        self.abort_calls.append(prepared)
        self.resource_released = True
        prepared.state = SessionProvisionState.ABORTED


class SessionCreateServer(AgentWebSocketServer):
    async def handle_session_create_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_session_create(ws, request, send_lock)


def make_server(runtime: CreateRuntime) -> SessionCreateServer:
    server = SessionCreateServer.__new__(SessionCreateServer)
    server._adapter_registry = AdapterRegistry()
    server._agent_manager = runtime.agent_manager
    server._runtime = runtime
    return server


def create_request() -> AgentRequest:
    return AgentRequest(
        request_id="create-request",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        user_id="envelope-user",
        metadata={"trace_id": "must-not-be-echoed"},
        params={
            "previous_session_id": "previous-session",
            "create_token": "create-token",
            "persist_session": True,
            "mode": "agent",
            "previous_mode": "team",
            "is_swarm": False,
            "team": {"name": "requested-team"},
            "project_id": "requested-project",
            "project_dir": "/requested/project",
            "cwd": "/requested/cwd",
            "work_mode": "work",
            "_work_mode_explicit": False,
            "title": "Create title",
            "user_id": "params-user",
            "model_name": "model-alias",
            "cron_id": "cron-id",
            "view_id": "view-7",
        },
    )


def test_session_create_handler_is_runtime_adapter_only() -> None:
    """Prevent Session business ownership from returning to AgentServer."""

    source = textwrap.dedent(
        inspect.getsource(AgentWebSocketServer._handle_session_create)
    )
    handler = ast.parse(source).body[0]
    assert isinstance(handler, ast.AsyncFunctionDef)

    called_attributes = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    called_names = {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    referenced_names = {
        node.id for node in ast.walk(handler) if isinstance(node, ast.Name)
    }
    referenced_attributes = {
        node.attr for node in ast.walk(handler) if isinstance(node, ast.Attribute)
    }

    assert {
        "_execution_runtime",
        "prepare_session_create",
        "commit_session_provision",
        "abort_session_provision",
    } <= called_attributes
    assert {
        "SessionCreateInput",
        "SessionProvisionCommitContext",
        "encode_agent_response_for_wire",
        "send_wire_payload",
    } <= called_names
    assert {"SessionProvisionCommitTiming", "send_lock"} <= referenced_names
    assert "AFTER_RESULT_DELIVERY" in referenced_attributes

    forbidden = {
        "_agent_manager",
        "_dispatch_session_switch_kvc",
        "_is_session_prewarm_model_eligible",
        "_prepare_session_switch_owner",
        "_session_switch_locks",
        "activate_session_prewarm",
        "claim_prewarmed_session",
        "find_or_create_code_project_for_tui_params",
        "get_agent_sessions_dir",
        "get_project_by_id",
        "get_session_metadata",
        "get_team_manager",
        "init_session_metadata",
        "is_valid_session_id",
        "project_store",
        "release_session_prewarm_claim",
        "resolve_request_runtime_mode",
        "resolve_session_project_binding",
        "resolve_session_work_mode_params",
        "WarmClaim",
    }
    assert forbidden.isdisjoint(
        called_attributes | called_names | referenced_names | referenced_attributes
    )


@pytest.mark.asyncio
async def test_success_preserves_claim_metadata_team_send_kvc_order() -> None:
    trace: list[str] = []
    runtime = CreateRuntime(trace)
    server = make_server(runtime)
    ws = RecordingWebSocket(trace)
    request = create_request()

    await server.handle_session_create_for_test(ws, request, asyncio.Lock())
    await asyncio.wait_for(runtime.commit_entered.wait(), timeout=1.0)

    assert trace == [
        "runtime.start",
        "create.claim",
        "create.metadata",
        "create.team",
        "response.send",
        "create.kvc",
    ]
    assert runtime.inputs == [
        SessionCreateInput(
            channel_id="web",
            previous_session_id="previous-session",
            create_token="create-token",
            persist_session=True,
            persist_session_supplied=True,
            mode="agent",
            previous_mode="team",
            is_swarm=False,
            team_hint=True,
            project_id="requested-project",
            project_dir="/requested/project",
            cwd="/requested/cwd",
            work_mode="work",
            work_mode_explicit=False,
            title="Create title",
            user_id="envelope-user",
            model_name="model-alias",
            cron_id="cron-id",
        )
    ]
    assert runtime.contexts == [
        SessionProvisionCommitContext(foreground_scope_id="view-7")
    ]
    assert runtime.abort_calls == []
    assert request.params["project_id"] == "resolved-project"
    assert request.params["project_dir"] == "/resolved/project"
    assert request.params["work_mode"] == "work"
    assert request.params["mode"] == "agent"
    assert "_work_mode_explicit" not in request.params

    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "create-request"
    assert response.channel_id == "web"
    assert response.ok is True
    assert response.payload == {
        "sessionId": "created-session",
        "session_id": "created-session",
        "projectId": "resolved-project",
        "projectDir": "/resolved/project",
        "workMode": "work",
        "persist_session": True,
        "prewarm_hit": True,
        "prewarm_status": "ready",
    }
    assert response.metadata is None


@pytest.mark.asyncio
async def test_send_failure_aborts_prepared_create_without_starting_kvc() -> None:
    trace: list[str] = []
    runtime = CreateRuntime(trace)
    server = make_server(runtime)
    send_error = OSError("send failed")
    ws = RecordingWebSocket(trace, send_outcomes=[send_error, None])

    await server.handle_session_create_for_test(
        ws,
        create_request(),
        asyncio.Lock(),
    )

    assert runtime.commit_calls == []
    assert runtime.abort_calls == runtime.prepared
    assert runtime.resource_released is True
    assert runtime.prepared[0].state is SessionProvisionState.ABORTED
    assert "create.kvc" not in trace
    assert trace.count("response.send") == 2
    error_response = parse_agent_server_wire_unary(ws.sent[0])
    assert error_response.ok is False
    assert error_response.payload == {"error": "send failed"}
    assert error_response.metadata is None


@pytest.mark.asyncio
async def test_cancelled_send_aborts_lease_and_propagates_same_cancellation() -> None:
    trace: list[str] = []
    runtime = CreateRuntime(trace)
    server = make_server(runtime)
    cancellation = asyncio.CancelledError("cancel create delivery")
    ws = RecordingWebSocket(trace, send_outcomes=[cancellation])

    with pytest.raises(asyncio.CancelledError) as captured:
        await server.handle_session_create_for_test(
            ws,
            create_request(),
            asyncio.Lock(),
        )

    assert captured.value is cancellation
    assert runtime.commit_calls == []
    assert runtime.abort_calls == runtime.prepared
    assert runtime.resource_released is True
    assert runtime.prepared[0].state is SessionProvisionState.ABORTED
    assert "create.kvc" not in trace
    assert ws.sent == []


@pytest.mark.asyncio
async def test_runtime_start_failure_preserves_internal_work_mode_marker() -> None:
    trace: list[str] = []
    runtime = CreateRuntime(trace)
    server = make_server(runtime)
    ws = RecordingWebSocket(trace)
    request = create_request()

    async def fail_start() -> None:
        raise RuntimeError("runtime start failed")

    runtime.start = fail_start
    await server.handle_session_create_for_test(ws, request, asyncio.Lock())

    assert request.params["_work_mode_explicit"] is False
    assert runtime.inputs == []
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is False
    assert response.payload == {"error": "runtime start failed"}


@pytest.mark.asyncio
async def test_runtime_prepare_failure_preserves_internal_work_mode_marker() -> None:
    trace: list[str] = []
    runtime = CreateRuntime(trace)
    server = make_server(runtime)
    ws = RecordingWebSocket(trace)
    request = create_request()

    async def fail_prepare(_provision_input: SessionCreateInput) -> Any:
        raise RuntimeError("runtime prepare failed")

    runtime.prepare_session_create = fail_prepare
    await server.handle_session_create_for_test(ws, request, asyncio.Lock())

    assert request.params["_work_mode_explicit"] is False
    assert runtime.prepared == []
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is False
    assert response.payload == {"error": "runtime prepare failed"}
