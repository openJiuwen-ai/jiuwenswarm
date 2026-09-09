# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer external behavior around the shared Runtime boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import SessionProvisionState
from jiuwenswarm.server.agent_ws_server import AdapterRegistry, AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter import (
    interface_deep as interface_deep_module,
)


RUNTIME_EXTRACTED = hasattr(AgentWebSocketServer, "_execution_runtime")


class RecordingWebSocket:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        self.trace.append("response.send")
        self.sent.append(json.loads(payload))


class EmptyConnectionWebSocket(RecordingWebSocket):
    remote_address = ("127.0.0.1", 19001)

    def __aiter__(self) -> EmptyConnectionWebSocket:
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def send(self, payload: str) -> None:
        frame = json.loads(payload)
        self.trace.append(
            "connection.ack"
            if frame.get("event") == "connection.ack"
            else "business.send"
        )
        self.sent.append(frame)


class OneMessageConnectionWebSocket(EmptyConnectionWebSocket):
    def __init__(
        self,
        trace: list[str],
        handler_started: asyncio.Event,
    ) -> None:
        super().__init__(trace)
        self.handler_started = handler_started
        self.input_sent = False

    async def __anext__(self) -> str:
        if not self.input_sent:
            self.input_sent = True
            return "{}"
        await self.handler_started.wait()
        raise StopAsyncIteration


class RecordingTaskMap(dict[str, Any]):
    def __init__(self, trace: list[str]) -> None:
        super().__init__({"stale-session": {}})
        self.trace = trace

    def clear(self) -> None:
        self.trace.append("session_tasks.clear")
        super().clear()


class CharacterizationServer(AgentWebSocketServer):
    _adapter_registry = AdapterRegistry()

    async def handle_message_for_test(
        self,
        ws: Any,
        raw: str,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_message(ws, raw, send_lock)

    async def handle_session_switch_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_session_switch(ws, request, send_lock)

    async def handle_session_fork_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        await self._handle_session_fork(ws, request, send_lock)


class RecordingAgent:
    def __init__(
        self,
        trace: list[str],
        *,
        interaction_error: str | None = None,
        cancel_error: str | None = None,
    ) -> None:
        self.trace = trace
        self.interaction_error = interaction_error
        self.cancel_error = cancel_error

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        if request.req_method == ReqMethod.CHAT_ANSWER:
            self.trace.append("interaction.execute")
            if self.interaction_error is not None:
                raise RuntimeError(self.interaction_error)
            payload = {"accepted": True, "resolved": False}
        elif request.req_method == ReqMethod.CHAT_CANCEL:
            self.trace.append("cancel.execute")
            if self.cancel_error is not None:
                raise RuntimeError(self.cancel_error)
            payload = {
                "event_type": "chat.interrupt_result",
                "success": True,
            }
        else:  # pragma: no cover - guards the characterization fixture itself
            raise AssertionError(f"unexpected request method: {request.req_method}")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
            agent_ref=request.agent_ref,
        )


class RecordingAgentManager:
    def __init__(
        self,
        trace: list[str],
        *,
        agent: RecordingAgent | None,
        cleanup_error: str | None = None,
    ) -> None:
        self.trace = trace
        self.agent = agent
        self.cleanup_error = cleanup_error
        self.cleanup_calls: list[tuple[str, str]] = []

    def get_agent_nowait(self, *_args: Any, **_kwargs: Any) -> RecordingAgent | None:
        return self.agent

    async def get_agent(self, **_kwargs: Any) -> RecordingAgent | None:
        return self.agent

    async def begin_foreground_chat(self) -> None:
        return None

    async def end_foreground_chat(self) -> None:
        return None

    async def cleanup_session_runtime(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> bool:
        self.trace.append("session.cleanup")
        self.cleanup_calls.append((channel_id, session_id))
        if self.cleanup_error is not None:
            raise RuntimeError(self.cleanup_error)
        return True


class PortableRuntime:
    """Small structural double understood only by the post-extraction server."""

    def __init__(self, manager: RecordingAgentManager, trace: list[str]) -> None:
        self.agent_manager = manager
        self.trace = trace
        self.create_calls: list[tuple[str, str | None]] = []
        self.fork_inputs: list[Any] = []
        self.fork_error: ValueError | None = None

    async def start(self) -> None:
        self.trace.append("runtime.start")

    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None = None,
    ) -> str:
        self.create_calls.append((channel_id, session_id))
        return session_id or "runtime-created-session"

    async def prepare_session_fork(self, provision_input: Any) -> Any:
        self.trace.append("fork.prepare")
        self.fork_inputs.append(provision_input)
        if self.fork_error is not None:
            raise self.fork_error
        target_session_id = (
            provision_input.target_session_id or "runtime-created-session"
        )
        return SimpleNamespace(
            state=SessionProvisionState.PREPARED,
            result=SimpleNamespace(
                session_id=target_session_id,
                source_session_id=provision_input.source_session_id,
                title=provision_input.title,
            )
        )

    async def commit_session_provision(
        self,
        prepared: Any,
        *,
        timing: Any,
    ) -> Any:
        del timing
        self.trace.append("fork.commit")
        prepared.state = SessionProvisionState.COMMITTED
        return prepared.result

    async def abort_session_provision(self, prepared: Any) -> None:
        self.trace.append("fork.abort")
        prepared.state = SessionProvisionState.ABORTED

    async def invoke(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Any = None,
    ) -> list[SimpleNamespace]:
        del trigger_hook, on_control_event
        assert self.agent_manager.agent is not None
        try:
            response = await self.agent_manager.agent.process_message(request)
        except Exception as exc:  # mirrors RuntimeEvent.error at the boundary
            return [
                SimpleNamespace(
                    event_type="runtime.error",
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    ok=False,
                    payload={"error": str(exc)},
                    is_complete=True,
                    metadata=None,
                    agent_ref=request.agent_ref,
                )
            ]
        return [
            SimpleNamespace(
                event_type="",
                request_id=response.request_id,
                channel_id=response.channel_id,
                session_id=request.session_id,
                ok=response.ok,
                payload=response.payload,
                is_complete=True,
                metadata=response.metadata,
                agent_ref=response.agent_ref,
            )
        ]

    async def answer_interaction(
        self,
        request: AgentRequest,
        *,
        trigger_hook: bool = True,
        on_control_event: Any = None,
    ) -> list[SimpleNamespace]:
        return await self.invoke(
            request,
            trigger_hook=trigger_hook,
            on_control_event=on_control_event,
        )

    async def cancel_request(
        self,
        request: AgentRequest,
        *,
        allow_create: bool = False,
    ) -> AgentResponse:
        del allow_create
        assert self.agent_manager.agent is not None
        return await self.agent_manager.agent.process_message(request)

    async def cancel_all_inflight_work(
        self,
        reason: str,
        *,
        exclude_session_ids: set[str] | tuple[str, ...] | None = None,
    ) -> None:
        await self.agent_manager.cancel_all_inflight_work(
            reason,
            exclude_session_ids=exclude_session_ids,
        )

    async def cancel_all_team_stream_tasks(
        self,
        reason: str,
        *,
        exclude_session_ids: set[str] | tuple[str, ...] | None = None,
    ) -> None:
        from jiuwenswarm.agents.harness.team import (
            cancel_all_team_stream_tasks_across_managers,
        )

        await cancel_all_team_stream_tasks_across_managers(
            reason=reason,
            exclude_session_ids=(
                None if exclude_session_ids is None else set(exclude_session_ids)
            ),
        )

    async def cleanup_session(self, *, channel_id: str, session_id: str) -> bool:
        return await self.agent_manager.cleanup_session_runtime(
            channel_id=channel_id,
            session_id=session_id,
        )


class NoopAdmission:
    async def begin_user(self, _session_id: str) -> None:
        return None

    async def end_user(self, _session_id: str) -> None:
        return None


def make_server(
    trace: list[str],
    *,
    agent: RecordingAgent | None,
    cleanup_error: str | None = None,
) -> tuple[CharacterizationServer, RecordingAgentManager, PortableRuntime]:
    manager = RecordingAgentManager(
        trace,
        agent=agent,
        cleanup_error=cleanup_error,
    )
    runtime = PortableRuntime(manager, trace)
    server = CharacterizationServer.__new__(CharacterizationServer)
    server._adapter_registry = AdapterRegistry()
    server._agent_manager = manager
    server._runtime = runtime
    server._session_stream_tasks = {}
    server._heartbeat_runtime = SimpleNamespace(admission=NoopAdmission())
    return server, manager, runtime


def request_wire(
    *,
    request_id: str,
    method: ReqMethod,
    session_id: str,
    params: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    envelope = e2a_from_agent_fields(
        request_id=request_id,
        channel_id="tui",
        session_id=session_id,
        req_method=method,
        params=params,
        is_stream=False,
        timestamp=0.0,
        metadata=metadata,
    )
    return json.dumps(envelope.to_dict(), ensure_ascii=False)


def configure_message_path(
    monkeypatch: pytest.MonkeyPatch,
    server: CharacterizationServer,
    trace: list[str],
    agent: RecordingAgent,
) -> None:
    async def ensure_checkpointer() -> None:
        return None

    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def before_hook(request: AgentRequest) -> None:
        if request.req_method == ReqMethod.CHAT_ANSWER:
            trace.append("interaction.before_hook")

    async def bind(request: AgentRequest) -> None:
        if request.req_method == ReqMethod.CHAT_ANSWER:
            trace.append("interaction.auto_binding")

    async def prepare_turn(
        _request: AgentRequest,
        _channel_id: str,
        *,
        sync_metadata: bool = True,
    ) -> tuple[str, None, RecordingAgent]:
        del sync_metadata
        return "agent", None, agent

    async def ensure_state(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        ensure_checkpointer,
    )
    monkeypatch.setattr(server, "_try_start_heartbeat_runtime", noop_async)
    monkeypatch.setattr(server, "_trigger_before_chat_request_hook", before_hook)
    monkeypatch.setattr(server, "_ensure_auto_team_binding_for_chat", bind)
    monkeypatch.setattr(server, "_prepare_code_mode_chat_turn", prepare_turn)
    monkeypatch.setattr(server, "_ensure_code_mode_state", ensure_state)
    monkeypatch.setattr(server, "_check_post_process_plan_exit", noop_async)
    monkeypatch.setattr(server, "_record_kvc_chat_started", noop_async)
    monkeypatch.setattr(
        server,
        "_record_kvc_chat_finished",
        lambda *_args, **_kwargs: None,
    )


async def install_blocking_stream_task(
    server: CharacterizationServer,
    trace: list[str],
    session_id: str,
) -> asyncio.Task[None]:
    started = asyncio.Event()
    block = asyncio.Event()

    async def stream_task() -> None:
        started.set()
        try:
            await block.wait()
        finally:
            trace.append("stream.settle")

    task = asyncio.create_task(stream_task())
    await started.wait()
    server._session_stream_tasks = {
        session_id: {task: asyncio.Event()},
    }
    return task


@pytest.mark.asyncio
async def test_session_switch_missing_id_keeps_bad_request_contract() -> None:
    trace: list[str] = []
    server, _, _ = make_server(trace, agent=None)
    ws = RecordingWebSocket(trace)
    request = AgentRequest(
        request_id="switch-missing-id",
        channel_id="web",
        req_method=ReqMethod.SESSION_SWITCH,
        params={},
        metadata={"trace_id": "switch-contract"},
    )

    await server.handle_session_switch_for_test(ws, request, asyncio.Lock())

    assert trace == ["response.send"]
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "switch-missing-id"
    assert response.channel_id == "web"
    assert response.ok is False
    assert response.payload == {
        "error": "session_id is required",
        "code": "BAD_REQUEST",
    }
    assert response.metadata == {"trace_id": "switch-contract"}


@pytest.mark.asyncio
async def test_session_fork_preserves_business_order_and_complete_result() -> None:
    trace: list[str] = []
    server, _, runtime = make_server(trace, agent=None)
    ws = RecordingWebSocket(trace)
    request = AgentRequest(
        request_id="fork-success",
        channel_id="tui",
        req_method=ReqMethod.SESSION_FORK,
        params={
            "source_session_id": "fork-source",
            "target_session_id": "fork-target",
            "title": "Forked session",
        },
        metadata={"trace_id": "fork-contract"},
    )

    await server.handle_session_fork_for_test(ws, request, asyncio.Lock())

    expected = ["fork.prepare", "fork.commit", "response.send"]
    if hasattr(server, "_execution_runtime"):
        expected.insert(0, "runtime.start")
    assert trace == expected
    assert runtime.create_calls == []
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "fork-success"
    assert response.channel_id == "tui"
    assert response.ok is True
    assert response.payload == {
        "session_id": "fork-target",
        "source_session_id": "fork-source",
        "title": "Forked session",
    }
    # session.fork historically does not echo request metadata.
    assert response.metadata is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "fork_error", "expected_error", "expected_code"),
    [
        ({}, None, "source_session_id is required", "BAD_REQUEST"),
        (
            {
                "source_session_id": "missing-source",
                "target_session_id": "fork-target",
            },
            "source session not found",
            "source session not found",
            "NOT_FOUND",
        ),
        (
            {
                "source_session_id": "fork-source",
                "target_session_id": "existing-target",
            },
            "target session already exists",
            "target session already exists",
            "ALREADY_EXISTS",
        ),
    ],
)
async def test_session_fork_preserves_value_error_mapping(
    params: dict[str, Any],
    fork_error: str | None,
    expected_error: str,
    expected_code: str,
) -> None:
    trace: list[str] = []
    server, _, runtime = make_server(trace, agent=None)
    ws = RecordingWebSocket(trace)
    runtime.fork_error = ValueError(fork_error) if fork_error is not None else None
    request = AgentRequest(
        request_id=f"fork-error-{expected_code.lower()}",
        channel_id="tui",
        req_method=ReqMethod.SESSION_FORK,
        params=params,
    )

    await server.handle_session_fork_for_test(ws, request, asyncio.Lock())

    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is False
    assert response.payload == {
        "error": expected_error,
        "code": expected_code,
    }


@pytest.mark.asyncio
async def test_chat_answer_preserves_external_order_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    agent = RecordingAgent(trace)
    server, _, _ = make_server(trace, agent=agent)
    configure_message_path(monkeypatch, server, trace, agent)
    ws = RecordingWebSocket(trace)

    await server.handle_message_for_test(
        ws,
        request_wire(
            request_id="interaction-success",
            method=ReqMethod.CHAT_ANSWER,
            session_id="interaction-session",
            params={
                "request_id": "question-1",
                "answers": [{"selected_options": ["Approve"]}],
            },
            metadata={"trace_id": "interaction-contract"},
        ),
        asyncio.Lock(),
    )

    assert trace == [
        "interaction.before_hook",
        "interaction.auto_binding",
        "interaction.execute",
        "response.send",
    ]
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "interaction-success"
    assert response.channel_id == "tui"
    assert response.ok is True
    assert response.payload == {"accepted": True, "resolved": False}
    assert response.metadata == {"trace_id": "interaction-contract"}


@pytest.mark.asyncio
async def test_chat_answer_exception_keeps_legacy_unary_error_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    agent = RecordingAgent(trace, interaction_error="interaction failed")
    server, _, _ = make_server(trace, agent=agent)
    configure_message_path(monkeypatch, server, trace, agent)
    if RUNTIME_EXTRACTED:
        from jiuwenswarm.runtime import AgentRuntime
        from jiuwenswarm.runtime.plan import PlanStateResult

        class InteractionPlanController:
            async def ensure_state(
                self,
                *_args: Any,
            ) -> PlanStateResult:
                return PlanStateResult()

            async def check_post_process_exit(
                self,
                *_args: Any,
            ) -> list[dict[str, Any]]:
                return []

            def reset_session(self, _session_id: str) -> None:
                return None

        class InteractionRuntime(AgentRuntime):
            async def prepare_chat_turn(
                self,
                _request: AgentRequest,
                _channel_id: str,
                *,
                sync_metadata: bool = True,
            ) -> tuple[str, None, RecordingAgent]:
                del sync_metadata
                return "agent", None, agent

        async def initialize() -> None:
            return None

        server._runtime = InteractionRuntime(
            agent_manager=server._agent_manager,
            initializer=initialize,
            plan_controller=InteractionPlanController(),
        )
    ws = RecordingWebSocket(trace)

    await server.handle_message_for_test(
        ws,
        request_wire(
            request_id="interaction-error",
            method=ReqMethod.CHAT_ANSWER,
            session_id="interaction-session",
            params={"request_id": "question-2", "answers": []},
            metadata={"trace_id": "interaction-error-contract"},
        ),
        asyncio.Lock(),
    )

    assert trace == [
        "interaction.before_hook",
        "interaction.auto_binding",
        "interaction.execute",
        "response.send",
    ]
    assert ws.sent[0]["response_kind"] == "e2a.error"
    assert ws.sent[0]["status"] == "failed"
    assert ws.sent[0]["is_final"] is True
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "interaction-error"
    assert response.channel_id == "tui"
    assert response.ok is False
    assert response.payload == {"error": "interaction failed"}
    assert response.metadata == {"trace_id": "interaction-error-contract"}


@pytest.mark.asyncio
async def test_manual_cancel_sends_result_without_cleaning_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    agent = RecordingAgent(trace)
    server, manager, _ = make_server(trace, agent=agent)
    configure_message_path(monkeypatch, server, trace, agent)
    ws = RecordingWebSocket(trace)
    stream_task = await install_blocking_stream_task(
        server,
        trace,
        "cancel-session",
    )

    await server.handle_message_for_test(
        ws,
        request_wire(
            request_id="manual-cancel",
            method=ReqMethod.CHAT_CANCEL,
            session_id="cancel-session",
            params={"intent": "cancel", "session_id": "cancel-session"},
        ),
        asyncio.Lock(),
    )

    assert trace == ["cancel.execute", "response.send", "stream.settle"]
    assert stream_task.done() is True
    assert manager.cleanup_calls == []
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is True
    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "success": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_error", "expected_ok", "expected_payload"),
    [
        (
            None,
            True,
            {"event_type": "chat.interrupt_result", "success": True},
        ),
        (
            "cleanup failed",
            False,
            {
                "event_type": "chat.interrupt_result",
                "success": False,
                "error": "session runtime cleanup failed",
            },
        ),
    ],
)
async def test_disconnect_cancel_cleans_before_reply_and_preserves_failure_protocol(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: str | None,
    expected_ok: bool,
    expected_payload: dict[str, Any],
) -> None:
    trace: list[str] = []
    agent = RecordingAgent(trace)
    server, manager, _ = make_server(
        trace,
        agent=agent,
        cleanup_error=cleanup_error,
    )
    configure_message_path(monkeypatch, server, trace, agent)
    ws = RecordingWebSocket(trace)
    stream_task = await install_blocking_stream_task(
        server,
        trace,
        "disconnect-session",
    )
    envelope = e2a_from_agent_fields(
        request_id="disconnect-cancel",
        channel_id="tui",
        session_id="disconnect-session",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel", "session_id": "disconnect-session"},
        is_stream=False,
        timestamp=0.0,
    )
    envelope.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"

    await server.handle_message_for_test(
        ws,
        json.dumps(envelope.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert trace == [
        "cancel.execute",
        "stream.settle",
        "session.cleanup",
        "response.send",
    ]
    assert stream_task.done() is True
    assert manager.cleanup_calls == [("tui", "disconnect-session")]
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.ok is expected_ok
    assert response.payload == expected_payload


@pytest.mark.asyncio
async def test_disconnect_cancel_failure_still_cleans_before_single_legacy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    agent = RecordingAgent(trace, cancel_error="cancel failed")
    server, manager, _ = make_server(trace, agent=agent)
    configure_message_path(monkeypatch, server, trace, agent)
    ws = RecordingWebSocket(trace)
    stream_task = await install_blocking_stream_task(
        server,
        trace,
        "disconnect-cancel-error-session",
    )
    envelope = e2a_from_agent_fields(
        request_id="disconnect-cancel-error",
        channel_id="tui",
        session_id="disconnect-cancel-error-session",
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "intent": "cancel",
            "session_id": "disconnect-cancel-error-session",
        },
        is_stream=False,
        timestamp=0.0,
        metadata={"trace_id": "disconnect-cancel-error-trace"},
    )
    envelope.channel_context["_jiuwenswarm_cancel_source"] = "client_disconnect"

    await server.handle_message_for_test(
        ws,
        json.dumps(envelope.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert trace == [
        "cancel.execute",
        "stream.settle",
        "session.cleanup",
        "response.send",
    ]
    assert stream_task.done() is True
    assert manager.cleanup_calls == [("tui", "disconnect-cancel-error-session")]
    assert len(ws.sent) == 1
    assert ws.sent[0]["response_kind"] == "e2a.error"
    assert ws.sent[0]["status"] == "failed"
    assert ws.sent[0]["is_final"] is True
    response = parse_agent_server_wire_unary(ws.sent[0])
    assert response.request_id == "disconnect-cancel-error"
    assert response.channel_id == "tui"
    assert response.ok is False
    assert response.payload == {"error": "cancel failed"}
    assert response.metadata == {
        "trace_id": "disconnect-cancel-error-trace",
        "_jiuwenswarm_cancel_source": "client_disconnect",
    }


@pytest.mark.asyncio
async def test_physical_disconnect_keeps_ack_only_and_cleanup_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    trace: list[str] = []
    server, manager, _ = make_server(trace, agent=None)
    ws = EmptyConnectionWebSocket(trace)
    server._session_stream_tasks = RecordingTaskMap(trace)
    server._heartbeat_runtime = SimpleNamespace(
        protocol_version="heartbeat-test-v1",
        is_available=True,
        execution=SimpleNamespace(
            active_session_ids=lambda: ("heartbeat-session",),
        ),
    )

    def clear_capabilities(actual_ws: Any) -> None:
        assert actual_ws is ws
        trace.append("capabilities.clear")

    async def cancel_all_inflight_work(
        reason: str,
        *,
        exclude_session_ids: tuple[str, ...],
    ) -> None:
        assert "gateway ws closed" in reason
        assert exclude_session_ids == ("heartbeat-session",)
        trace.append("manager.cancel_all")

    async def stop_scheduler() -> None:
        trace.append("scheduler.stop")

    async def cancel_team_streams(
        *,
        reason: str,
        exclude_session_ids: set[str],
    ) -> None:
        assert "gateway ws closed" in reason
        assert exclude_session_ids == {"heartbeat-session"}
        trace.append("team_streams.cancel_all")

    monkeypatch.setattr(
        server,
        "_clear_ws_acp_client_capabilities",
        clear_capabilities,
    )
    monkeypatch.setattr(
        manager,
        "cancel_all_inflight_work",
        cancel_all_inflight_work,
        raising=False,
    )
    monkeypatch.setattr(server, "_stop_scheduler", stop_scheduler)
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        cancel_team_streams,
    )

    await server._connection_handler(ws)

    assert trace == [
        "connection.ack",
        "capabilities.clear",
        "manager.cancel_all",
        "scheduler.stop",
        "team_streams.cancel_all",
        "session_tasks.clear",
    ]
    assert ws.sent == [
        {
            "type": "event",
            "event": "connection.ack",
            "payload": {
                "status": "ready",
                "heartbeat_job_owner": "agentserver",
                "heartbeat_job_protocol": "heartbeat-test-v1",
                "heartbeat_job_ready": True,
            },
        }
    ]
    assert server._current_ws is None
    assert server._current_send_lock is None
    assert server._session_stream_tasks == {}


@pytest.mark.asyncio
async def test_physical_disconnect_runtime_cancel_failure_keeps_cleaning_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_module

    trace: list[str] = []
    handler_started = asyncio.Event()
    never_finish = asyncio.Event()
    server, _, runtime = make_server(trace, agent=None)
    ws = OneMessageConnectionWebSocket(trace, handler_started)
    server._session_stream_tasks = RecordingTaskMap(trace)
    server._heartbeat_runtime = SimpleNamespace(
        protocol_version="heartbeat-test-v1",
        is_available=True,
        execution=SimpleNamespace(
            active_session_ids=lambda: ("heartbeat-session",),
        ),
    )

    async def blocking_handler(*_args: Any) -> None:
        trace.append("connection_task.start")
        handler_started.set()
        try:
            await never_finish.wait()
        finally:
            trace.append("connection_task.settle")

    def clear_capabilities(actual_ws: Any) -> None:
        assert actual_ws is ws
        trace.append("capabilities.clear")

    async def failing_runtime_cancel_all(
        reason: str,
        *,
        exclude_session_ids: tuple[str, ...],
    ) -> None:
        assert "gateway ws closed" in reason
        assert exclude_session_ids == ("heartbeat-session",)
        trace.append("runtime.cancel_all")
        raise RuntimeError("runtime cancel all failed")

    async def stop_scheduler() -> None:
        trace.append("scheduler.stop")

    async def cancel_team_streams(
        *,
        reason: str,
        exclude_session_ids: set[str],
    ) -> None:
        assert "gateway ws closed" in reason
        assert exclude_session_ids == {"heartbeat-session"}
        trace.append("team_streams.cancel_all")

    monkeypatch.setattr(server, "_handle_message", blocking_handler)
    monkeypatch.setattr(
        server,
        "_clear_ws_acp_client_capabilities",
        clear_capabilities,
    )
    monkeypatch.setattr(
        runtime,
        "cancel_all_inflight_work",
        failing_runtime_cancel_all,
    )
    monkeypatch.setattr(server, "_stop_scheduler", stop_scheduler)
    monkeypatch.setattr(
        team_module,
        "cancel_all_team_stream_tasks_across_managers",
        cancel_team_streams,
    )

    await server._connection_handler(ws)

    assert trace == [
        "connection.ack",
        "connection_task.start",
        "capabilities.clear",
        "runtime.cancel_all",
        "scheduler.stop",
        "team_streams.cancel_all",
        "connection_task.settle",
        "session_tasks.clear",
    ]
    assert ws.sent == [
        {
            "type": "event",
            "event": "connection.ack",
            "payload": {
                "status": "ready",
                "heartbeat_job_owner": "agentserver",
                "heartbeat_job_protocol": "heartbeat-test-v1",
                "heartbeat_job_ready": True,
            },
        }
    ]
    assert server._current_ws is None
    assert server._current_send_lock is None
    assert server._session_stream_tasks == {}
