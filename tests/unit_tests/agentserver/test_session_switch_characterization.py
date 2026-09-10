# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavior contract for ``session.switch`` across the Runtime boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from weakref import WeakValueDictionary

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import (
    AgentRuntime,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionState,
    SessionSwitchInput,
    SessionSwitchResult,
)
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AdapterRegistry, AgentWebSocketServer


class RecordingWebSocket:
    def __init__(
        self,
        trace: list[str] | None = None,
        *,
        send_error: Exception | None = None,
    ) -> None:
        self.trace = trace if trace is not None else []
        self.sent: list[dict[str, Any]] = []
        self.send_error = send_error

    async def send(self, payload: str) -> None:
        self.trace.append("response.send")
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(payload))


class SwitchRuntime:
    """Small Runtime-boundary double; it contains no Server business logic."""

    def __init__(
        self,
        trace: list[str] | None = None,
        *,
        result_mode: str = "agent.plan",
    ) -> None:
        self.agent_manager = object()
        self.trace = trace if trace is not None else []
        self.result_mode = result_mode
        self.start_count = 0
        self.inputs: list[SessionSwitchInput] = []
        self.contexts: list[SessionProvisionCommitContext] = []
        self.prepared: list[Any] = []
        self.abort_calls: list[Any] = []
        self.prepare_hook: Any = None
        self.commit_hook: Any = None

    async def start(self) -> None:
        self.start_count += 1

    async def prepare_session_switch(
        self,
        provision_input: SessionSwitchInput,
    ) -> Any:
        self.trace.append("switch.prepare")
        self.inputs.append(provision_input)
        if self.prepare_hook is not None:
            await self.prepare_hook(provision_input)
        prepared = SimpleNamespace(
            state=SessionProvisionState.PREPARED,
            result=SessionSwitchResult(
                channel_id=provision_input.channel_id,
                session_id=provision_input.target_session_id,
                mode=self.result_mode,
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
    ) -> SessionSwitchResult:
        assert timing is SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
        self.trace.append("switch.kvc")
        self.contexts.append(context)
        prepared.state = SessionProvisionState.COMMITTING
        if self.commit_hook is not None:
            try:
                await self.commit_hook(context)
            except BaseException:
                prepared.state = SessionProvisionState.COMMITTED
                raise
        prepared.state = SessionProvisionState.COMMITTED
        return prepared.result

    async def abort_session_provision(self, prepared: Any) -> None:
        self.abort_calls.append(prepared)
        prepared.state = SessionProvisionState.ABORTED


class SessionSwitchServer(AgentWebSocketServer):
    async def handle_session_switch_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_session_switch(ws, request, send_lock)

    async def handle_message_for_test(
        self,
        ws: Any,
        raw: str,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_message(ws, raw, send_lock)

    async def _handle_gateway_cron_callback(self, *_args: Any) -> bool:
        return False

    async def _dispatch_gateway_adapter_request(self, *_args: Any) -> bool:
        return False

    async def _trigger_before_chat_request_hook(
        self,
        _request: AgentRequest,
    ) -> None:
        return None


@pytest.fixture(autouse=True)
def isolated_switch_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_ws_server_module,
        "_session_switch_locks",
        WeakValueDictionary(),
    )


def make_server(runtime: Any = None) -> SessionSwitchServer:
    runtime = runtime or SwitchRuntime()
    server = SessionSwitchServer.__new__(SessionSwitchServer)
    server._adapter_registry = AdapterRegistry()
    server._agent_manager = runtime.agent_manager
    server._runtime = runtime
    return server


def switch_request(
    *,
    request_id: str = "switch-request",
    channel_id: str | None = "web",
    session_id: str = "request-session",
    params: Any = None,
    metadata: dict[str, Any] | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        req_method=ReqMethod.SESSION_SWITCH,
        params={} if params is None else params,
        metadata=metadata,
    )


def switch_wire(
    *,
    request_id: str,
    session_id: str,
    params: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> str:
    envelope = e2a_from_agent_fields(
        request_id=request_id,
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.SESSION_SWITCH,
        params=params,
        is_stream=False,
        timestamp=0.0,
        metadata=metadata,
    )
    return json.dumps(envelope.to_dict(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_success_preserves_complete_wire_metadata_and_order() -> None:
    trace: list[str] = []
    runtime = SwitchRuntime(trace, result_mode="code.normal")
    server = make_server(runtime)
    ws = RecordingWebSocket(trace)
    metadata = {"trace_id": "switch-success", "nested": {"value": 1}}
    request = switch_request(
        request_id="switch-success",
        session_id="request-session",
        params={
            "session_id": "target-session",
            "previous_session_id": "previous-session",
            "mode": "code.normal",
            "previous_mode": "team",
            "team": {"name": "requested-team"},
            "view_id": "view-7",
        },
        metadata=metadata,
    )

    await server.handle_session_switch_for_test(ws, request, asyncio.Lock())

    assert trace == ["switch.prepare", "switch.kvc", "response.send"]
    assert runtime.inputs == [
        SessionSwitchInput(
            channel_id="web",
            target_session_id="target-session",
            previous_session_id="previous-session",
            mode="code.normal",
            previous_mode="team",
            team_hint=True,
        )
    ]
    assert runtime.contexts == [
        SessionProvisionCommitContext(foreground_scope_id="view-7")
    ]
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "switch-success"
    assert response.channel_id == "web"
    assert response.ok is True
    assert response.payload == {
        "session_id": "target-session",
        "mode": "code.normal",
        "switched": True,
    }
    assert response.metadata == metadata


@pytest.mark.asyncio
async def test_server_crosses_real_runtime_and_provisioner_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.session.kv_cache import (
        kv_cache_product_hooks,
    )

    trace: list[str] = []

    class AgentManagerStub:
        async def cancel_all_inflight_work(self, _reason: str) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    class PlanControllerStub:
        def reset_session(self, _session_id: str) -> None:
            return None

    async def initialize() -> None:
        trace.append("runtime.start")

    context = SimpleNamespace(
        target_is_team=False,
        previous_is_team=False,
        resolved_mode="code.normal",
        affinity_enabled=True,
    )

    def resolve_context(**_kwargs: Any) -> Any:
        trace.append("runtime.switch.prepare")
        return context

    async def dispatch_signals(**kwargs: Any) -> None:
        trace.append("runtime.switch.commit")
        assert kwargs["view_id"] == "integration-view"

    monkeypatch.setattr(
        kv_cache_product_hooks,
        "resolve_session_switch_context",
        resolve_context,
    )
    monkeypatch.setattr(
        kv_cache_product_hooks,
        "dispatch_session_switch_signals",
        dispatch_signals,
    )
    runtime = AgentRuntime(
        agent_manager=AgentManagerStub(),  # type: ignore[arg-type]
        initializer=initialize,
        plan_controller=PlanControllerStub(),  # type: ignore[arg-type]
    )
    server = make_server(runtime)
    ws = RecordingWebSocket(trace)

    try:
        await server.handle_session_switch_for_test(
            ws,
            switch_request(
                params={
                    "session_id": "integration-session",
                    "mode": "agent.plan",
                    "view_id": "integration-view",
                }
            ),
            asyncio.Lock(),
        )

        assert trace == [
            "runtime.start",
            "runtime.switch.prepare",
            "runtime.switch.commit",
            "response.send",
        ]
        response = parse_agent_server_wire_unary(ws.sent[0])
        assert response.payload == {
            "session_id": "integration-session",
            "mode": "code.normal",
            "switched": True,
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "params",
        "request_session_id",
        "channel_id",
        "expected_target",
        "expected_channel",
    ),
    [
        (
            {"session_id": "params-session", "previous_session_id": "old"},
            "request-session",
            "web",
            "params-session",
            "web",
        ),
        (
            {"previous_session_id": "old"},
            "request-session",
            None,
            "request-session",
            "default",
        ),
        (
            ["not", "a", "mapping"],
            "request-session",
            "tui",
            "request-session",
            "tui",
        ),
    ],
)
async def test_target_precedence_fallback_and_default_channel(
    params: Any,
    request_session_id: str,
    channel_id: str | None,
    expected_target: str,
    expected_channel: str,
) -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    ws = RecordingWebSocket()
    request = switch_request(
        channel_id=channel_id,
        session_id=request_session_id,
        params=params,
    )

    await server.handle_session_switch_for_test(ws, request, asyncio.Lock())

    assert runtime.inputs[0].target_session_id == expected_target
    assert runtime.inputs[0].channel_id == expected_channel
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.channel_id == (channel_id or "")
    assert response.payload == {
        "session_id": expected_target,
        "mode": "agent.plan",
        "switched": True,
    }


@pytest.mark.asyncio
async def test_missing_target_stays_server_validation_with_exact_error() -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    ws = RecordingWebSocket()
    metadata = {"trace_id": "missing-target"}

    await server.handle_session_switch_for_test(
        ws,
        switch_request(
            session_id="  ",
            params={"session_id": "  "},
            metadata=metadata,
        ),
        asyncio.Lock(),
    )

    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is False
    assert response.payload == {
        "error": "session_id is required",
        "code": "BAD_REQUEST",
    }
    assert response.metadata == metadata
    assert runtime.start_count == 0
    assert runtime.inputs == []


@pytest.mark.asyncio
async def test_default_view_id_remains_scoped_to_server_websocket() -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    ws = RecordingWebSocket()

    await server.handle_session_switch_for_test(
        ws,
        switch_request(params={"session_id": "target-session"}),
        asyncio.Lock(),
    )

    assert runtime.contexts == [
        SessionProvisionCommitContext(
            foreground_scope_id=f"ws:{id(ws)}",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["prepare", "kvc"])
async def test_business_failure_keeps_legacy_error_wire_through_full_message_path(
    failure_stage: str,
) -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    error_message = f"{failure_stage} failed"

    async def fail_prepare(_provision_input: SessionSwitchInput) -> None:
        raise RuntimeError(error_message)

    commit_attempts = 0

    async def fail_commit(
        _context: SessionProvisionCommitContext,
    ) -> None:
        nonlocal commit_attempts
        commit_attempts += 1
        raise RuntimeError(error_message)

    if failure_stage == "prepare":
        runtime.prepare_hook = fail_prepare
    else:
        runtime.commit_hook = fail_commit
    metadata = {"trace_id": f"switch-{failure_stage}-failure"}
    ws = RecordingWebSocket()

    await server.handle_message_for_test(
        ws,
        switch_wire(
            request_id=f"switch-{failure_stage}-failure",
            session_id="request-session",
            params={"session_id": "target-session"},
            metadata=metadata,
        ),
        asyncio.Lock(),
    )

    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == f"switch-{failure_stage}-failure"
    assert response.channel_id == "tui"
    assert response.ok is False
    assert response.payload == {"error": error_message}
    assert response.metadata == metadata
    if failure_stage == "kvc":
        assert commit_attempts == 1
        assert runtime.prepared[0].state is SessionProvisionState.COMMITTED


@pytest.mark.asyncio
async def test_cancelled_prepare_releases_switch_lock_for_successor() -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    ws = RecordingWebSocket()
    owner_entered = asyncio.Event()
    successor_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def prepare(provision_input: SessionSwitchInput) -> None:
        if provision_input.target_session_id == "owner-session":
            owner_entered.set()
            await never_release.wait()
        successor_entered.set()

    runtime.prepare_hook = prepare
    owner: asyncio.Task[None] | None = None
    successor: asyncio.Task[None] | None = None
    try:
        owner = asyncio.create_task(
            server.handle_session_switch_for_test(
                ws,
                switch_request(
                    request_id="switch-owner",
                    params={"session_id": "owner-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(owner_entered.wait(), timeout=1.0)
        lock_key = f"{id(ws)}:web"
        owner_lock = agent_ws_server_module._session_switch_locks[lock_key]
        assert owner_lock.locked()

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert not owner_lock.locked()

        successor = asyncio.create_task(
            server.handle_session_switch_for_test(
                ws,
                switch_request(
                    request_id="switch-successor",
                    params={"session_id": "successor-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(successor_entered.wait(), timeout=1.0)
        await successor
        assert parse_agent_server_wire_unary(ws.sent[0]).payload == {
            "session_id": "successor-session",
            "mode": "agent.plan",
            "switched": True,
        }
    finally:
        never_release.set()
        tasks = [task for task in (owner, successor) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_commit_is_terminal_and_does_not_strand_lease() -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    ws = RecordingWebSocket()
    commit_entered = asyncio.Event()
    never_release = asyncio.Event()
    commit_attempts = 0

    async def commit(_context: SessionProvisionCommitContext) -> None:
        nonlocal commit_attempts
        commit_attempts += 1
        commit_entered.set()
        await never_release.wait()

    runtime.commit_hook = commit
    task = asyncio.create_task(
        server.handle_session_switch_for_test(
            ws,
            switch_request(params={"session_id": "owner-session"}),
            asyncio.Lock(),
        )
    )
    try:
        await asyncio.wait_for(commit_entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert commit_attempts == 1
        assert runtime.prepared[0].state is SessionProvisionState.COMMITTED
        assert runtime.abort_calls == []
        assert ws.sent == []
        lock_key = f"{id(ws)}:web"
        assert not agent_ws_server_module._session_switch_locks[lock_key].locked()
    finally:
        never_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_websocket", [False, True])
async def test_independent_switch_lock_scopes_can_prepare_concurrently(
    same_websocket: bool,
) -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    first_ws = RecordingWebSocket()
    second_ws = first_ws if same_websocket else RecordingWebSocket()
    second_channel = "tui" if same_websocket else "web"
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def prepare(provision_input: SessionSwitchInput) -> None:
        if provision_input.target_session_id == "first-session":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()

    runtime.prepare_hook = prepare
    first: asyncio.Task[None] | None = None
    second: asyncio.Task[None] | None = None
    try:
        first = asyncio.create_task(
            server.handle_session_switch_for_test(
                first_ws,
                switch_request(
                    request_id="switch-first",
                    channel_id="web",
                    params={"session_id": "first-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=1.0)

        second = asyncio.create_task(
            server.handle_session_switch_for_test(
                second_ws,
                switch_request(
                    request_id="switch-second",
                    channel_id=second_channel,
                    params={"session_id": "second-session"},
                ),
                asyncio.Lock(),
            )
        )
        await asyncio.wait_for(second_entered.wait(), timeout=1.0)
        assert not first.done()

        release_first.set()
        await asyncio.gather(first, second)
    finally:
        release_first.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_websocket_and_channel_remain_serialized() -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    ws = RecordingWebSocket()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    prepare_order: list[str] = []

    async def prepare(provision_input: SessionSwitchInput) -> None:
        prepare_order.append(provision_input.target_session_id)
        if provision_input.target_session_id == "first-session":
            first_entered.set()
            await release_first.wait()

    runtime.prepare_hook = prepare
    first = asyncio.create_task(
        server.handle_session_switch_for_test(
            ws,
            switch_request(params={"session_id": "first-session"}),
            asyncio.Lock(),
        )
    )
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=1.0)
        second = asyncio.create_task(
            server.handle_session_switch_for_test(
                ws,
                switch_request(params={"session_id": "second-session"}),
                asyncio.Lock(),
            )
        )
        await asyncio.sleep(0)
        assert prepare_order == ["first-session"]

        release_first.set()
        await asyncio.gather(first, second)
        assert prepare_order == ["first-session", "second-session"]
    finally:
        release_first.set()
        tasks = [first] + ([second] if second is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_failure_does_not_abort_committed_switch() -> None:
    runtime = SwitchRuntime()
    server = make_server(runtime)
    send_error = RuntimeError("send failed")
    ws = RecordingWebSocket(send_error=send_error)

    with pytest.raises(RuntimeError) as captured:
        await server.handle_session_switch_for_test(
            ws,
            switch_request(params={"session_id": "target-session"}),
            asyncio.Lock(),
        )

    assert captured.value is send_error
    assert runtime.prepared[0].state is SessionProvisionState.COMMITTED
    assert runtime.abort_calls == []
