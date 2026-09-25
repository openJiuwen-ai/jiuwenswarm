# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durability, ordering, isolation, and admission tests for Session messaging."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    SessionMessagingRouteRail,
    SessionMessagingRoute,
    SessionMessagingToolkit,
    bind_session_messaging_route,
    current_session_messaging_route,
    reset_session_messaging_route,
    session_messaging_route_context,
    with_session_messaging_route,
)
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import (
    SessionRunAdmission,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult,
    SessionMessageService,
    SessionMessageSource,
    SessionMessagingError,
)
from jiuwenswarm.server.runtime.session.session_message_store import (
    SessionMessageIdempotencyConflict,
    SessionMessageStore,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.request import sync_chat_request_metadata
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.runtime.session import (
    RuntimeSessionCoordinator,
    SessionPersistencePolicy,
    SessionWorkKind,
)
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.runtime.session.execution_registry import SessionExecutionRegistry
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import (
    AgentWebSocketServer,
    _strip_untrusted_session_message_context,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.common.session_message import (
    SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY,
    SESSION_MESSAGE_INTERNAL_KEY,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ask_user_interrupt", "permission_interrupt"])
@pytest.mark.parametrize("cancel_stream", [False, True])
@pytest.mark.parametrize("valid_correlation", [False, True])
async def test_cross_session_question_can_be_answered(
    source: str, cancel_stream: bool, valid_correlation: bool
) -> None:
    """Keep valid questions answerable and release abandoned interactions."""

    class _Agent:
        async def process_message_stream(self, request):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": "permission-1" if valid_correlation else "",
                    "source": source,
                    "questions": [{"question": "Allow deletion?"}],
                },
                is_complete=True,
            )
            if cancel_stream:
                await asyncio.Event().wait()

        async def deliver_control_input(self, request):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "answered"},
                is_complete=True,
            )

    agent = _Agent()
    manager = SimpleNamespace(
        get_agent_for_session_nowait=Mock(return_value=agent),
        cleanup=AsyncMock(),
        cancel_all_inflight_work=AsyncMock(),
        cleanup_session_runtime=AsyncMock(return_value=True),
    )
    plan = SimpleNamespace(
        ensure_state=AsyncMock(return_value=PlanStateResult()),
        check_post_process_exit=AsyncMock(return_value=[]),
        reset_session=Mock(),
    )
    coordinator = RuntimeSessionCoordinator()
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=plan,
        session_coordinator=coordinator,
        admission_controller=SessionRunAdmission(),
    )

    async def prepare(request, channel_id, **kwargs):
        return "agent", "normal", agent

    runtime._prepare_chat_turn = prepare
    await runtime.start()
    await coordinator.register_session(
        "target-1", "web", SessionPersistencePolicy.PERSISTENT
    )
    try:
        incoming = AgentRequest(
            request_id="session-message-1",
            channel_id="web",
            session_id="target-1",
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={
                "query": "delete a file",
                "mode": "agent.work.normal",
                SESSION_MESSAGE_INTERNAL_KEY: {"message_id": "sm-1"},
            },
        )
        stream = runtime.stream(incoming, background=True, trigger_hook=False)
        if cancel_stream:
            events = [await anext(stream)]
            await stream.aclose()
            assert [event.event_type for event in events] == ["chat.ask_user_question"]
            assert not runtime._admission_controller.has_pending_interaction("target-1")
            return
        events = [event async for event in stream]
        assert [event.event_type for event in events] == ["chat.ask_user_question"]
        if not valid_correlation:
            assert not runtime._admission_controller.has_pending_interaction("target-1")
            return
        execution = coordinator.snapshot_session("target-1").executions[-1]
        assert execution.work_kind is SessionWorkKind.SESSION_MESSAGE
        assert execution.state is SessionExecutionState.WAITING_FOR_CONTROL
        assert execution.waiting_control_id == "permission-1"

        answer = AgentRequest(
            request_id="answer-1",
            channel_id="web",
            session_id="target-1",
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={
                "query": "",
                "mode": "agent.work.normal",
                "request_id": "permission-1",
                "source": source,
                "answers": [{"selected_options": ["approve"]}],
            },
        )
        resumed = [event async for event in runtime.stream(answer)]
        assert [event.event_type for event in resumed] == ["chat.final"]
        assert (
            coordinator.get_execution(execution.execution_id).state
            is SessionExecutionState.SUCCEEDED
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_up, history_evicted", [(False, False), (True, False), (True, True)])
async def test_plain_user_turn_releases_superseded_session_question(
    follow_up: bool, history_evicted: bool,
) -> None:
    coordinator = RuntimeSessionCoordinator(
        registry=SessionExecutionRegistry(terminal_capacity=0) if history_evicted else None
    )
    await coordinator.register_session(
        "target-1", "web", SessionPersistencePolicy.PERSISTENT
    )
    admission = SessionRunAdmission()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._session_coordinator = coordinator
    runtime._admission_controller = admission
    runtime._session_message_service = SimpleNamespace(
        supersede_waiting_for_target=AsyncMock(return_value=1)
    )
    try:
        await coordinator.run_unary(
            "target-1", "session-message-1", SessionWorkKind.SESSION_MESSAGE,
            lambda: asyncio.sleep(0),
            suspension_key=lambda _: "permission-1",
        )
        waiting = coordinator.snapshot_session("target-1").executions[-1]
        assert waiting.state is SessionExecutionState.WAITING_FOR_CONTROL
        await admission.mark_interaction_pending("target-1", "permission-1")
        if follow_up:
            await coordinator.deliver_control(
                "target-1", "permission-1",
                lambda: asyncio.sleep(0),
                suspension_key=lambda _: "permission-2",
            )
            await admission.clear_interaction_pending("target-1", "permission-1")
            await admission.mark_interaction_pending("target-1", "permission-2")
            waiting = coordinator.snapshot_session("target-1").executions[-1]
            assert waiting.state is SessionExecutionState.WAITING_FOR_CONTROL
            if history_evicted:
                assert coordinator.get_execution(waiting.parent_execution_id) is None

        await runtime._supersede_bypassed_session_messages(AgentRequest(
            request_id="user-1",
            channel_id="web",
            session_id="target-1",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "do something else"},
        ))

        cancelled = coordinator.get_execution(waiting.execution_id)
        if history_evicted:
            assert cancelled is None
        else:
            assert cancelled.state is SessionExecutionState.CANCELLED
        assert not admission.has_pending_interaction("target-1")
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id, include_content", [("web", True), ("tui", False)])
async def test_session_message_status_content_is_web_only(monkeypatch, channel_id, include_content):
    record = SimpleNamespace(
        message_id="sm-1",
        execution_request_id="",
        owner_scope_id="owner",
        source_session_id="source-1",
        source_title_snapshot="Source",
        target_session_id="target-1",
        content="private prompt",
        chain_id="chain-1",
        hop_count=0,
        status="queued",
        input_mode="",
        created_at="now",
        started_at=None,
        finished_at=None,
        last_error_code="",
        last_error="",
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "build_server_push_message",
        lambda **kwargs: {
            **kwargs,
            "channel_id": channel_id,
            "metadata": {SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY: True},
        },
    )
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    pushed = []

    async def send_push(message):
        pushed.append(message)
        return True

    server.send_push = send_push
    await server._push_session_message_status(record)

    message = pushed[0]
    assert message["metadata"]["_jiuwenswarm_session_message_owner_scope_id"] == "owner"
    assert message["metadata"][SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY] is False
    assert ("content" in message["payload"]["message"]) is include_content


@pytest.mark.asyncio
@pytest.mark.parametrize("target_user_id", ["", "local"])
async def test_session_message_status_distinguishes_anonymous_local_owner(monkeypatch, target_user_id):
    record = SimpleNamespace(
        message_id="sm-1", execution_request_id="", owner_scope_id="local",
        source_session_id="source-1", source_title_snapshot="Source",
        target_session_id="target-1", content="private prompt",
        chain_id="chain-1", hop_count=0, status="queued", input_mode="", created_at="now",
        started_at=None, finished_at=None, last_error_code="", last_error="",
    )
    monkeypatch.setattr(
        agent_ws_server_module, "get_session_metadata",
        lambda *args, **kwargs: {"user_id": target_user_id},
    )
    monkeypatch.setattr(
        agent_ws_server_module, "build_server_push_message",
        lambda **kwargs: {**kwargs, "channel_id": "web"},
    )
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    pushed = []

    async def send_push(message):
        pushed.append(message)
        return True

    server.send_push = send_push
    await server._push_session_message_status(record)
    assert pushed[0]["metadata"][SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY] is not bool(target_user_id)


async def _wait_for_status(
    store: SessionMessageStore,
    message_id: str,
    status: str,
    *,
    timeout: float = 5.0,
) -> None:
    """轮询等待消息状态到位（时间封顶）。

    worker 的 transition_status 走 asyncio.to_thread，事件循环里的
    sleep(0) 轮询迭代数封顶在慢机器上等不到线程池往返；改为真实
    小步 sleep + wait_for 超时兜底。
    """

    async def _check() -> None:
        while True:
            record = store.get(message_id)
            if record is not None and record.status == status:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_check(), timeout=timeout)


def _enqueue(store: SessionMessageStore, *, key: str, content: str = "check"):
    return store.enqueue(
        owner_scope_id="user-1",
        source_session_id="source-1",
        source_title_snapshot="Source",
        source_request_id="request-1",
        source_tool_call_id=key,
        idempotency_key=key,
        target_session_id="target-1",
        content=content,
        hop_count=1,
    )


def test_store_deduplicates_claims_and_quarantines_inflight_after_restart(
    tmp_path,
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    first, created = _enqueue(store, key="call-1")
    duplicate, duplicate_created = _enqueue(store, key="call-1")

    assert created is True
    assert duplicate_created is False
    assert duplicate.message_id == first.message_id

    with pytest.raises(SessionMessageIdempotencyConflict):
        _enqueue(store, key="call-1", content="different")

    claimed = store.claim(first.message_id, "execution-1", "run-1")
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.runtime_run_id == "run-1"

    restarted = SessionMessageStore(store.path)
    assert restarted.recover_inflight_as_unknown() == 1
    recovered = restarted.get(first.message_id)
    assert recovered is not None
    assert recovered.status == "unknown"
    assert restarted.next_queued("target-1") is None


def test_store_migrates_existing_mailbox_for_interrupt_correlation(tmp_path) -> None:
    path = tmp_path / "messages.sqlite3"
    SessionMessageStore(path).ensure_schema()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "ALTER TABLE session_messages DROP COLUMN interrupt_request_id"
        )
        conn.execute("ALTER TABLE session_messages DROP COLUMN interrupt_source")
        conn.execute("ALTER TABLE session_messages DROP COLUMN queue_released_at")

    SessionMessageStore(path).ensure_schema()

    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(session_messages)")
        }
    assert {"interrupt_request_id", "interrupt_source", "queue_released_at"} <= columns


class _RecordingAdmission:
    def __init__(self) -> None:
        self.active: set[str] = set()

    async def begin_session_message(self, session_id: str, run_id: str) -> None:
        assert session_id not in self.active
        self.active.add(session_id)

    async def end_session_message(self, session_id: str, run_id: str) -> None:
        self.active.discard(session_id)

    def is_user_active(self, session_id: str) -> bool:
        return False

    def is_session_message_active(self, session_id: str) -> bool:
        return session_id in self.active


def _source(call: str) -> SessionMessageSource:
    return SessionMessageSource(
        session_id="source-1",
        request_id="request-1",
        tool_call_id=call,
        idempotency_key=call,
        user_id="user-1",
    )


def _metadata(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "title": session_id.title(),
        "user_id": "user-1",
        "channel_id": "web",
        "mode": "agent.code.normal",
        "project_id": "project-1",
    }


async def _waiting_mailbox(tmp_path, *, key: str = "waiting-resume"):
    store = SessionMessageStore(tmp_path / f"{key}.sqlite3")
    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    await service.start()
    waiting, _ = _enqueue(store, key=key)
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="tool-call-first",
        interrupt_source="ask_user_interrupt",
    )
    return store, service, waiting


@pytest.mark.asyncio
async def test_service_persists_while_disconnected_then_runs_target_fifo(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    admission = _RecordingAdmission()
    first_started = asyncio.Event()
    allow_first = asyncio.Event()
    all_finished = asyncio.Event()
    executed: list[str] = []

    async def execute(record):
        executed.append(record.content)
        if len(executed) == 1:
            first_started.set()
            await allow_first.wait()
        if len(executed) == 2:
            all_finished.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=admission,
        execute=execute,
        available=False,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)

    first = await service.send_message(
        _source("call-1"), input_mode="follow_up", target_session_id="target-1", message="first"
    )
    await asyncio.sleep(0)
    assert executed == []
    assert store.get(first["message_id"]).status == "queued"

    await service.set_available(True)
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = await service.send_message(
        _source("call-2"), input_mode="follow_up", target_session_id="target-1", message="second"
    )
    await asyncio.sleep(0)
    assert executed == ["first"]

    allow_first.set()
    await asyncio.wait_for(all_finished.wait(), timeout=1)
    await _wait_for_status(store, first["message_id"], "succeeded")
    await _wait_for_status(store, second["message_id"], "succeeded")

    assert executed == ["first", "second"]
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["interaction", "goal"])
async def test_busy_target_accepts_messages_without_executing_until_released(
    tmp_path, monkeypatch, blocker,
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    admission = SessionRunAdmission()
    blocked = True
    if blocker == "interaction":
        await admission.mark_interaction_pending("target-1", "plan-confirm")
    else:
        admission.set_session_message_blocker(lambda sid: sid == "target-1" and blocked)
    executed = []

    async def execute(record):
        executed.append(record.content)
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store, admission=admission, execute=execute, available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    monkeypatch.setattr(service, "_metadata", lambda: ([_metadata("target-1")], 1))
    try:
        first = await asyncio.wait_for(service.send_message(
            _source("first"), input_mode="follow_up", target_session_id="target-1", message="first",
        ), 1)
        second = await asyncio.wait_for(service.send_message(
            _source("second"), input_mode="follow_up", target_session_id="target-1", message="second",
        ), 1)
        listed = await service.list_targets(_source("list"))
        assert listed["sessions"][0]["runtime_state"] == "busy"
        assert listed["sessions"][0]["pending_message_count"] == 2
        assert first["accepted"] is True
        assert first["status"] == second["status"] == "queued"
        assert executed == []
        assert store.get(first["message_id"]).status == "queued"
        blocked = False
        await admission.clear_interaction_pending("target-1", "plan-confirm")
        await _wait_for_status(store, second["message_id"], "succeeded")
        assert executed == ["first", "second"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_uncertain_execution_blocks_later_messages_without_replay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    attempted = asyncio.Event()

    async def execute(record):
        attempted.set()
        raise RuntimeError("transport vanished after side effect")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    first = await service.send_message(
        _source("uncertain-1"), input_mode="follow_up", target_session_id="target-1", message="first"
    )
    second = await service.send_message(
        _source("uncertain-2"), input_mode="follow_up", target_session_id="target-1", message="second"
    )
    await asyncio.wait_for(attempted.wait(), timeout=1)
    await _wait_for_status(store, first["message_id"], "unknown")

    assert store.get(first["message_id"]).status == "unknown"
    assert store.get(second["message_id"]).status == "queued"
    assert store.next_queued("target-1") is None
    await service.stop()


@pytest.mark.asyncio
async def test_unconfirmed_execution_result_blocks_fifo_as_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    finished = asyncio.Event()

    async def execute(record):
        return SessionMessageExecutionResult(
            status="unknown",
            error_code="EXECUTION_NOT_COMPLETED",
            error="accepted without terminal event",
        )

    async def notify(record):
        if record.status == "unknown":
            finished.set()

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        status_callback=notify,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    first = await service.send_message(
        _source("ack-only-1"), input_mode="follow_up", target_session_id="target-1", message="first"
    )
    second = await service.send_message(
        _source("ack-only-2"), input_mode="follow_up", target_session_id="target-1", message="second"
    )
    await asyncio.wait_for(finished.wait(), timeout=1)

    assert store.get(first["message_id"]).status == "unknown"
    assert store.get(second["message_id"]).status == "queued"
    assert store.next_queued("target-1") is None
    await service.stop()


@pytest.mark.asyncio
async def test_unknown_message_can_be_explicitly_resolved_without_replay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    unknown, _ = _enqueue(store, key="unknown-1", content="uncertain")
    store.claim(unknown.message_id, "execution-1", "run-1")
    store.transition_status(
        unknown.message_id, "unknown", expected_statuses=("running",)
    )
    queued, _ = _enqueue(store, key="queued-after-unknown", content="next")

    listed = await service.list_messages(_source("list-messages"))
    assert [item["message_id"] for item in listed["messages"]] == [
        queued.message_id,
        unknown.message_id,
    ]
    assert "owner_scope_id" not in listed["messages"][0]
    assert "source_request_id" not in listed["messages"][0]
    assert "runtime_run_id" not in listed["messages"][0]

    resolved = await service.resolve_unknown(
        _source("resolve-unknown"),
        message_id=unknown.message_id,
        resolution="cancelled",
    )

    assert resolved["status"] == "cancelled"
    assert resolved["resolution"] == "cancelled_by_user"
    assert store.next_queued("target-1").message_id == queued.message_id
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("via_target_action", [False, True])
async def test_restart_continues_durable_queue_on_explicit_request_without_replaying_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch, via_target_action: bool
) -> None:
    path = tmp_path / "messages.sqlite3"
    store = SessionMessageStore(path)
    interrupted, _ = _enqueue(store, key="interrupted", content="may have run")
    store.claim(interrupted.message_id, "execution-1", "run-1")
    queued, _ = _enqueue(store, key="queued", content="run after restart")
    executed: list[str] = []

    async def execute(record):
        executed.append(record.message_id)
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=SessionMessageStore(path),
        admission=_RecordingAdmission(),
        execute=execute,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        await service.start()
        assert service.store.get(interrupted.message_id).status == "unknown"
        assert service.store.get(queued.message_id).status == "queued"
        assert service.store.next_queued("target-1") is None
        snapshot = await service.queued_for_target("target-1", "user-1")
        assert [item["message_id"] for item in snapshot] == [queued.message_id]

        if via_target_action:
            with pytest.raises(SessionMessagingError):
                await service.continue_queued_for_target("target-1", "other-user")
            continued = await service.continue_queued_for_target("target-1", "user-1")
            assert continued["released_unknown_count"] == 1
            repeated = await service.continue_queued_for_target("target-1", "user-1")
            assert repeated["released_unknown_count"] == 0
        else:
            continued = await service.resolve_unknown(
                _source("continue"),
                message_id=interrupted.message_id,
                resolution="continue_queued",
            )
        await _wait_for_status(service.store, queued.message_id, "succeeded")

        uncertain = service.store.get(interrupted.message_id)
        assert uncertain.status == "unknown"
        assert uncertain.resolved_at is None
        assert uncertain.queue_released_at is not None
        if not via_target_action:
            assert continued["queue_released_at"] == uncertain.queue_released_at
        assert executed == [queued.message_id]
        assert service.store.pending_counts(["target-1"]) == {}
        assert service.store.blocking_states(["target-1"]) == {}
        assert SessionMessageStore(path).get(interrupted.message_id).status == "unknown"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_restart_keeps_queued_message_idle_until_user_continues(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "messages.sqlite3"
    queued, _ = _enqueue(SessionMessageStore(path), key="queued-before-restart")
    executed: list[str] = []

    async def execute(record):
        executed.append(record.message_id)
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=SessionMessageStore(path),
        admission=_RecordingAdmission(),
        execute=execute,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        await service.start()
        await service.set_available(True)
        assert executed == []
        assert service.store.get(queued.message_id).status == "queued"
        snapshot = await service.queued_for_target("target-1", "user-1")
        assert [item["message_id"] for item in snapshot] == [queued.message_id]

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._session_message_service = service
        ws = SimpleNamespace(send=AsyncMock())
        await server._handle_session_message_continue_queued(
            ws,
            AgentRequest(
                request_id="continue-1",
                channel_id="web",
                session_id="target-1",
                req_method=ReqMethod.SESSION_MESSAGE_CONTINUE_QUEUED,
                params={"session_id": "target-1"},
                user_id="user-1",
            ),
            asyncio.Lock(),
        )
        response = json.loads(ws.send.await_args.args[0])
        assert response["body"]["result"]["accepted"] is True
        await _wait_for_status(service.store, queued.message_id, "succeeded")
        assert executed == [queued.message_id]
    finally:
        await service.stop()


@pytest.mark.parametrize("caller", ["source-1", "target-1", "third-session"])
@pytest.mark.parametrize("with_unknown", [False, True])
async def test_agent_can_discover_read_and_continue_after_restart(
    tmp_path, monkeypatch, caller, with_unknown
):
    from jiuwenswarm.server.runtime.session import session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    unknown = None
    if with_unknown:
        unknown, _ = _enqueue(store, key="uncertain")
        store.claim(unknown.message_id, "old-execution", "old-run")
    queued, _ = _enqueue(store, key="queued", content="pending work")
    executed = []
    history_path = tmp_path / "sessions" / "target-1" / "history.jsonl"
    history_path.parent.mkdir(parents=True)

    async def execute(record):
        executed.append(record.message_id)
        history_path.write_text(json.dumps({
            "id": "answer", "role": "assistant", "content": "task result",
            "event_type": "chat.final", "session_message_id": record.message_id,
        }) + "\n", encoding="utf-8")
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=SessionMessageStore(store.path), admission=_RecordingAdmission(), execute=execute
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    monkeypatch.setattr(service, "_metadata", lambda: ([_metadata("target-1")], 1))
    toolkit = SessionMessagingToolkit(service)
    rail = SessionMessagingRouteRail()
    route = SessionMessagingRoute(session_id=caller, request_id="continue-request", user_id="user-1")
    ctx = SimpleNamespace(
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="continue-call", name="session_continue_queued"),
            tool_name="session_continue_queued",
        ),
        extra={"run_context": with_session_messaging_route({}, route)["run"]["context"]},
    )
    await service.start()
    await service.set_available(True)
    await rail.before_tool_call(ctx)
    try:
        if caller != "target-1":
            targets = await toolkit.list_sessions()
            assert targets["sessions"][0]["queue_pause_reason"] == "host_restarted"
        mailbox = await toolkit.list_messages(target_session_id="target-1")
        assert queued.message_id in {item["message_id"] for item in mailbox["messages"]}
        before = await toolkit.read_session("target-1")
        assert before["queue"]["can_continue_queued"] is True
        assert before["messages"] == []
        assert executed == []

        replies = await asyncio.gather(
            toolkit.continue_queued("target-1"), toolkit.continue_queued("target-1")
        )
        assert all(reply["accepted"] for reply in replies)
        assert all(reply["finish_current_turn"] == (caller == "target-1") for reply in replies)
        await _wait_for_status(service.store, queued.message_id, "succeeded")
        after = await toolkit.read_session("target-1")
        assert after["messages"][0]["content"] == "task result"
        assert after["messages"][0]["session_message_id"] == queued.message_id
        assert after["queue"]["queue_paused"] is False
        assert executed == [queued.message_id]
        if unknown:
            assert service.store.get(unknown.message_id).status == "unknown"
    finally:
        await rail.after_tool_call(ctx)
        await service.stop()


async def test_read_session_history_is_bounded_paginated_and_target_scoped(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    history_path = tmp_path / "target-1" / "history.jsonl"
    history_path.parent.mkdir()
    records = [
        {"id": str(i), "role": "assistant", "content": "answer" * 10, "private_metadata": "omit"}
        for i in range(5)
    ]
    history_path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_RecordingAdmission(), execute=AsyncMock(),
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        first = await service.read_session(_source("read"), target_session_id="target-1", limit=2, max_output_chars=8)
        assert [item["id"] for item in first["messages"]] == ["4", "3"]
        assert all(item["truncated"] and len(item["content"]) == 8 for item in first["messages"])
        assert all("private_metadata" not in item for item in first["messages"])
        second = await service.read_session(
            _source("read"), target_session_id="target-1", limit=2, cursor=first["next_cursor"]
        )
        assert [item["id"] for item in second["messages"]] == ["2", "1"]
        with pytest.raises(SessionMessagingError, match="different session"):
            await service.read_session(_source("read"), target_session_id="other", cursor=first["next_cursor"])
        service._execute.assert_not_awaited()
    finally:
        await service.stop()


async def test_continue_current_session_defers_execution_until_user_turn_finishes(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="held")
    admission = SessionRunAdmission()
    executed = asyncio.Event()

    async def execute(record):
        executed.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(store=store, admission=admission, execute=execute)
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    await admission.begin_user("target-1")
    try:
        source = SessionMessagingRoute(
            session_id="target-1", request_id="user-continue", user_id="user-1"
        ).source_for_list()
        result = await service.continue_queued(source, target_session_id="target-1")
        assert result["finish_current_turn"] is True
        snapshot = await service.read_session(source, target_session_id="target-1")
        assert snapshot["queue"]["runtime_state"] == "busy"
        assert snapshot["queue"]["queue_paused"] is False
        assert store.get(queued.message_id).status == "queued"
        assert not executed.is_set()
        await admission.end_user("target-1")
        await asyncio.wait_for(executed.wait(), timeout=2)
        await _wait_for_status(store, queued.message_id, "succeeded")
    finally:
        await admission.end_user("target-1")
        await service.stop()


@pytest.mark.parametrize("operation", ["read_session", "continue_queued", "list_messages"])
@pytest.mark.parametrize("forbidden", ["source-1", "target-1"])
async def test_session_observation_and_continuation_check_both_owners(
    tmp_path, monkeypatch, operation, forbidden
):
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="private")
    service = SessionMessageService(store=store, admission=_RecordingAdmission(), execute=AsyncMock())
    monkeypatch.setattr(service, "_session_metadata", lambda sid: {
        **_metadata(sid), "user_id": "other-user" if sid == forbidden else "user-1",
    })
    try:
        with pytest.raises(SessionMessagingError) as exc:
            await getattr(service, operation)(_source("call"), target_session_id="target-1")
        assert exc.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert store.get(queued.message_id).status == "queued"
        service._execute.assert_not_awaited()
    finally:
        await service.stop()


async def test_pending_prompt_survives_restart_and_refreshes_without_replaying_content(tmp_path, monkeypatch):
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="old-pending", content="untrusted mailbox instructions")
    for index in range(55):
        record, _ = _enqueue(store, key=f"completed-{index}")
        store.claim(record.message_id, f"request-{index}", f"run-{index}")
        store.transition_status(record.message_id, "succeeded", expected_statuses=("running",))
    service = SessionMessageService(
        store=SessionMessageStore(store.path), admission=_RecordingAdmission(),
        execute=AsyncMock(return_value=SessionMessageExecutionResult(status="succeeded")),
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    rail = SessionMessagingRouteRail()
    builder = Mock()
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    rail.set_service(service)
    route = SessionMessagingRoute(session_id="source-1", request_id="query", user_id="user-1")
    ctx = SimpleNamespace(extra={"run_context": with_session_messaging_route({}, route)["run"]["context"]})
    try:
        await rail.before_model_call(ctx)
        content = builder.add_section.call_args.args[0].content["en"]
        state = json.loads(content.split("\n", 1)[1])
        assert state["queues"][0]["target_session_id"] == "target-1"
        assert state["queues"][0]["queue_pause_reason"] == "host_restarted"
        assert "untrusted mailbox instructions" not in content
        service._execute.assert_not_awaited()

        await service.continue_queued(route.source_for_list(), target_session_id="target-1")
        await _wait_for_status(service.store, queued.message_id, "succeeded")
        await rail.before_model_call(ctx)
        content = builder.add_section.call_args.args[0].content["en"]
        assert json.loads(content.split("\n", 1)[1])["queues"] == []

        builder.reset_mock()
        await rail.before_model_call(SimpleNamespace(extra={}))
        builder.remove_section.assert_called_once_with("session_tools")
        builder.add_section.assert_not_called()
    finally:
        rail.uninit(None)
        await service.stop()


@pytest.mark.asyncio
async def test_deleting_target_cancels_running_and_queued_messages(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    started = asyncio.Event()
    keep_running = asyncio.Event()

    async def execute(record):
        started.set()
        await keep_running.wait()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    running = await service.send_message(
        _source("delete-1"), input_mode="follow_up", target_session_id="target-1", message="running"
    )
    queued = await service.send_message(
        _source("delete-2"), input_mode="follow_up", target_session_id="target-1", message="queued"
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await service.on_target_deleted("target-1") == 2
    assert store.get(running["message_id"]).status == "cancelled"
    assert store.get(running["message_id"]).content == ""
    assert store.get(queued["message_id"]).status == "cancelled"
    assert store.get(queued["message_id"]).content == ""
    await service.stop()


@pytest.mark.asyncio
async def test_failed_target_delete_resumes_queued_consumer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    executed = asyncio.Event()

    async def execute(record):
        executed.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        available=False,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    sent = await service.send_message(
        _source("delete-abort"), input_mode="follow_up", target_session_id="target-1", message="queued"
    )
    await service.begin_target_delete("target-1")
    with pytest.raises(SessionMessagingError) as exc_info:
        await service.send_message(
            _source("delete-blocked"),
            input_mode="follow_up", target_session_id="target-1",
            message="too late",
        )
    assert exc_info.value.code == "NOT_FOUND_OR_FORBIDDEN"
    await service.set_available(True)
    await asyncio.sleep(0)
    assert not executed.is_set()
    assert store.get(sent["message_id"]).status == "queued"

    await service.abort_target_delete("target-1")
    await asyncio.wait_for(executed.wait(), timeout=1)
    await _wait_for_status(store, sent["message_id"], "succeeded")

    assert store.get(sent["message_id"]).status == "succeeded"
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_ok", [True, False])
async def test_agentserver_pauses_mailbox_around_online_delete(
    delete_ok: bool, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.session import (
        lifecycle as lc,
        project_store,
        session_metadata as sm,
    )
    from jiuwenswarm.server.runtime.session.session_archive import SessionArchiveService

    ordering = []

    class _Service:
        async def begin_target_delete(self, session_id):
            ordering.append(("pause", session_id))

        async def on_target_deleted(self, session_id):
            ordering.append(("commit", session_id))

        async def abort_target_delete(self, session_id):
            ordering.append(("abort", session_id))

    class _Runtime:
        session_message_service = None
        is_session_running = staticmethod(lambda session_id: False)

        async def stop_session_for_archive(self, **kwargs):
            return None

        async def delete_session(self, **kwargs):
            ordering.append(("delete", kwargs["session_id"]))
            return SimpleNamespace(
                ok=delete_ok,
                session_id=kwargs["session_id"],
                error_message="delete failed",
                error_code="DELETE_FAILED",
            )

    _Runtime.session_message_service = _Service()

    root = tmp_path / "agent"
    active = root / "sessions"
    active.mkdir(parents=True)
    for module in (lc, project_store):
        monkeypatch.setattr(module, "get_agent_root_dir", lambda: root)
    for module in (lc, sm):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: active)
    import jiuwenswarm.server.runtime.session.session_archive as archive_module

    monkeypatch.setattr(
        archive_module, "get_agent_sessions_dir", lambda: active
    )
    project_store.invalidate_cache()
    sm._METADATA_CACHE.clear()
    try:
        directory = active / "target-1"
        directory.mkdir()
        lc.atomic_json(
            directory / "metadata.json",
            dict(
                session_id="target-1",
                channel_id="web",
                title="target",
                work_mode="work",
                project_id="default",
            ),
        )
        service = SessionArchiveService(_Runtime())

        if delete_ok:
            await service.session("target-1", "delete", "web")
        else:
            with pytest.raises(lc.LifecycleError):
                await service.session("target-1", "delete", "web")

        expected_resolution = "commit" if delete_ok else "abort"
        assert ordering[0:3] == [
            ("pause", "target-1"),
            ("delete", "target-1"),
            (expected_resolution, "target-1"),
        ]
    finally:
        project_store.invalidate_cache()
        sm._METADATA_CACHE.clear()


@pytest.mark.asyncio
async def test_service_hides_cross_user_and_unsupported_targets(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        _metadata("source-1"),
        {**_metadata("target-1"), "last_message_at": 0},
        {**_metadata("other-user"), "user_id": "user-2"},
        {**_metadata("team-target"), "mode": "team.normal"},
    ]
    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    monkeypatch.setattr(service, "_metadata", lambda: (rows, len(rows)))
    monkeypatch.setattr(
        service,
        "_session_metadata",
        lambda session_id: next(
            (dict(row) for row in rows if row["session_id"] == session_id), {}
        ),
    )
    blocked, _ = _enqueue(service.store, key="unknown")
    service.store.claim(blocked.message_id, "execution-1", "run-1")
    service.store.transition_status(
        blocked.message_id, "unknown", expected_statuses=("running",)
    )

    listed = await service.list_targets(_source("list"))
    assert [item["session_id"] for item in listed["sessions"]] == ["target-1"]
    assert listed["sessions"][0]["runtime_state"] == "unknown"
    assert listed["sessions"][0]["last_message_at"] == "1970-01-01T00:00:00.000Z"

    with pytest.raises(SessionMessagingError) as exc_info:
        await service.send_message(
            _source("call-other"),
            input_mode="follow_up", target_session_id="other-user",
            message="secret",
        )
    assert exc_info.value.code == "NOT_FOUND_OR_FORBIDDEN"
    await service.stop()


@pytest.mark.asyncio
async def test_empty_user_id_only_matches_unowned_local_sessions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {**_metadata("source-1"), "user_id": ""},
        {**_metadata("local-target"), "user_id": ""},
        {**_metadata("owned-target"), "user_id": "user-2"},
    ]
    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    monkeypatch.setattr(service, "_metadata", lambda: (rows, len(rows)))
    monkeypatch.setattr(
        service,
        "_session_metadata",
        lambda session_id: next(
            (dict(row) for row in rows if row["session_id"] == session_id), {}
        ),
    )
    anonymous = SessionMessageSource(
        session_id="source-1",
        request_id="request-1",
        tool_call_id="anonymous",
        idempotency_key="anonymous",
        user_id="",
    )

    listed = await service.list_targets(anonymous)

    assert [item["session_id"] for item in listed["sessions"]] == ["local-target"]
    with pytest.raises(SessionMessagingError) as exc_info:
        await service.send_message(
            anonymous,
            input_mode="follow_up", target_session_id="owned-target",
            message="secret",
        )
    assert exc_info.value.code == "NOT_FOUND_OR_FORBIDDEN"
    await service.stop()


@pytest.mark.asyncio
async def test_agentserver_rechecks_anonymous_owner_before_execution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = store.enqueue(
        owner_scope_id="local",
        source_session_id="source-1",
        source_title_snapshot="Source",
        source_request_id="request-1",
        source_tool_call_id="anonymous-owner",
        idempotency_key="anonymous-owner",
        target_session_id="target-1",
        content="secret",
        hop_count=1,
    )
    record = store.claim(queued.message_id, "execution-1", "run-1")
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_session_metadata",
        lambda *args, **kwargs: {**_metadata("target-1"), "user_id": "user-2"},
    )

    result = await server.execute_internal_session_message(record)

    assert result.status == "failed"
    assert result.error_code == "NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.asyncio
async def test_transient_sqlite_claim_error_retries_without_dropping_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    executed = asyncio.Event()

    async def execute(record):
        executed.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    original_claim = store.claim
    attempts = 0

    def flaky_claim(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim", flaky_claim)
    sent = await service.send_message(
        _source("transient-lock"),
        input_mode="follow_up", target_session_id="target-1",
        message="retry me",
    )

    await asyncio.wait_for(executed.wait(), timeout=1)
    await _wait_for_status(store, sent["message_id"], "succeeded")

    assert attempts == 2
    assert store.get(sent["message_id"]).status == "succeeded"
    await service.stop()


@pytest.mark.asyncio
async def test_exhausted_claim_retry_keeps_message_queued_for_next_worker_loop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    executed = asyncio.Event()

    async def execute(record):
        executed.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    original_claim = store.claim
    attempts = 0

    def busy_claim(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise sqlite3.OperationalError("database is locked")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim", busy_claim)
    sent = await service.send_message(
        _source("exhausted-transient-lock"),
        input_mode="follow_up", target_session_id="target-1",
        message="retry on the next loop",
    )

    await asyncio.wait_for(executed.wait(), timeout=1)
    await _wait_for_status(store, sent["message_id"], "succeeded")

    assert attempts == 4
    assert store.get(sent["message_id"]).status == "succeeded"
    await service.stop()


@pytest.mark.asyncio
async def test_resumed_target_answer_unblocks_next_mailbox_item(tmp_path) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    await service.start()
    waiting, _ = _enqueue(store, key="waiting")
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="tool-call-1",
        interrupt_source="ask_user_interrupt",
    )
    _enqueue(store, key="queued", content="next")

    assert store.next_queued("target-1") is None
    completed = await service.complete_waiting_after_resume(
        "target-1",
        interrupt_request_id="tool-call-1",
        interrupt_source="ask_user_interrupt",
        waiting_user=False,
        failed=False,
    )

    assert completed is True
    assert store.get(waiting.message_id).status == "succeeded"
    assert store.next_queued("target-1").content == "next"
    await service.stop()


@pytest.mark.asyncio
async def test_repeated_user_question_replaces_resume_correlation(tmp_path) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    await service.start()
    waiting, _ = _enqueue(store, key="waiting-repeated")
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="tool-call-first",
        interrupt_source="ask_user_interrupt",
    )

    assert await service.complete_waiting_after_resume(
        "target-1",
        interrupt_request_id="tool-call-first",
        interrupt_source="ask_user_interrupt",
        waiting_user=True,
        failed=False,
        next_interrupt_request_id="tool-call-second",
        next_interrupt_source="permission_interrupt",
    ) is True
    assert store.waiting_for_resume(
        "target-1", "tool-call-first", "ask_user_interrupt"
    ) is None
    rebound = store.waiting_for_resume(
        "target-1", "tool-call-second", "permission_interrupt"
    )
    assert rebound is not None
    assert rebound.message_id == waiting.message_id
    await service.stop()


@pytest.mark.asyncio
async def test_unrelated_interrupt_answer_does_not_resolve_waiting_message(
    tmp_path,
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    await service.start()
    waiting, _ = _enqueue(store, key="waiting-correlated")
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="tool-call-expected",
        interrupt_source="permission_interrupt",
    )

    assert await service.complete_waiting_after_resume(
        "target-1",
        interrupt_request_id="tool-call-stale",
        interrupt_source="permission_interrupt",
        waiting_user=False,
        failed=False,
    ) is False
    assert store.get(waiting.message_id).status == "waiting_user"
    await service.stop()


@pytest.mark.asyncio
async def test_resume_completion_wins_race_with_original_waiting_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    waiting_persisted = asyncio.Event()
    allow_original_return = asyncio.Event()

    async def execute(record):
        assert await service.mark_waiting(
            record.message_id,
            interrupt_request_id="tool-call-1",
            interrupt_source="ask_user_interrupt",
        )
        waiting_persisted.set()
        await allow_original_return.wait()
        return SessionMessageExecutionResult(status="waiting_user")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    sent = await service.send_message(
        _source("waiting-race"), input_mode="follow_up", target_session_id="target-1", message="ask"
    )
    await asyncio.wait_for(waiting_persisted.wait(), timeout=1)

    assert await service.complete_waiting_after_resume(
        "target-1",
        interrupt_request_id="tool-call-1",
        interrupt_source="ask_user_interrupt",
        waiting_user=False,
        failed=False,
    ) is True
    allow_original_return.set()
    await _wait_for_status(store, sent["message_id"], "succeeded")

    assert store.get(sent["message_id"]).status == "succeeded"
    await service.stop()


@pytest.mark.asyncio
async def test_failure_after_question_does_not_leave_message_waiting(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    finished = asyncio.Event()

    async def execute(record):
        assert await service.mark_waiting(
            record.message_id,
            interrupt_request_id="tool-call-1",
            interrupt_source="ask_user_interrupt",
        )
        return SessionMessageExecutionResult(
            status="failed",
            error_code="EXECUTION_FAILED",
            error="failed after question",
        )

    async def notify(record):
        if record.status == "failed":
            finished.set()

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        status_callback=notify,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    sent = await service.send_message(
        _source("question-then-fail"),
        input_mode="follow_up", target_session_id="target-1",
        message="ask",
    )
    await asyncio.wait_for(finished.wait(), timeout=1)

    failed = store.get(sent["message_id"])
    assert failed.status == "failed"
    assert failed.last_error == "failed after question"
    await service.stop()


@pytest.mark.asyncio
async def test_session_message_admission_is_mutually_exclusive_with_user_turns() -> (
    None
):
    admission = SessionRunAdmission()
    await admission.begin_user("target-1")

    message_admitted = asyncio.Event()

    async def begin_message() -> None:
        await admission.begin_session_message("target-1", "run-1")
        message_admitted.set()

    message_task = asyncio.create_task(begin_message())
    await asyncio.sleep(0)
    assert not message_admitted.is_set()

    await admission.end_user("target-1")
    await asyncio.wait_for(message_admitted.wait(), timeout=1)

    user_task = asyncio.create_task(admission.begin_user("target-1"))
    await asyncio.sleep(0)
    assert not user_task.done()

    await admission.end_session_message("target-1", "run-1")
    await asyncio.wait_for(user_task, timeout=1)
    await admission.end_user("target-1")
    await message_task


@pytest.mark.asyncio
async def test_session_message_waits_for_matching_interaction_answer() -> None:
    admission = SessionRunAdmission()
    await admission.mark_interaction_pending("target-1", "plan-confirmation")
    message = asyncio.create_task(admission.begin_session_message("target-1", "run-1"))
    try:
        await asyncio.sleep(0)
        assert not message.done()
        await admission.clear_interaction_pending("target-1", "stale-answer")
        await asyncio.sleep(0)
        assert not message.done()
        # Answering remains possible while the mailbox waits. The continuation
        # must finish before external work is admitted.
        await admission.begin_user("target-1")
        await admission.clear_interaction_pending("target-1", "plan-confirmation")
        await asyncio.sleep(0)
        assert not message.done()
        await admission.end_user("target-1")
        await asyncio.wait_for(message, timeout=1)
    finally:
        message.cancel()
        await asyncio.gather(message, return_exceptions=True)
        await admission.end_session_message("target-1", "run-1")


def test_cross_session_request_does_not_touch_human_activity_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def sync_metadata(**kwargs):
        captured.update(kwargs)
        return kwargs.get("project_dir")

    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(
        session_metadata, "sync_session_request_metadata", sync_metadata
    )
    request = AgentRequest(
        request_id="execution-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "mode": "agent.code.normal",
            "_jiuwenswarm_cross_session": {"message_id": "sm-1"},
        },
    )

    sync_chat_request_metadata(request, "/project", "agent.code.normal")

    assert captured["is_chat_turn"] is False
    assert captured["last_user_message_at"] is None


def test_toolkit_uses_request_local_route_and_stable_replay_keys() -> None:
    route = SessionMessagingRoute(
        session_id="source-1",
        request_id="request-1",
        user_id="user-1",
    )
    first = route.source_for_call("target-1", "same", tool_call_id="tool-call-1")
    second = route.source_for_call("target-1", "same", tool_call_id="tool-call-2")
    replay = SessionMessagingRoute(
        session_id="source-1",
        request_id="request-1",
        user_id="user-1",
    ).source_for_call("target-1", "same", tool_call_id="tool-call-1")

    assert first.idempotency_key != second.idempotency_key
    assert replay.idempotency_key == first.idempotency_key
    with pytest.raises(SessionMessagingError) as exc_info:
        route.source_for_call("target-1", "same")
    assert exc_info.value.code == "MISSING_TOOL_CALL_ID"
    assert [tool.card.name for tool in SessionMessagingToolkit().get_tools()] == [
        "session_list",
        "session_send_message",
        "session_message_list",
        "session_continue_queued",
        "session_read",
        "session_message_resolve",
    ]

    token = bind_session_messaging_route(
        session_id="target-1",
        request_id="execution-1",
        user_id="user-1",
        cross_session={
            "message_id": "sm-1",
            "chain_id": "chain-1",
            "hop_count": 2,
        },
    )
    try:
        inherited = current_session_messaging_route()
        assert inherited is not None
        assert inherited.parent_message_id == "sm-1"
        assert inherited.chain_id == "chain-1"
        assert inherited.hop_count == 2
    finally:
        reset_session_messaging_route(token)


@pytest.mark.asyncio
async def test_toolkit_rail_keeps_concurrent_invocation_routes_isolated() -> None:
    captured = []
    both_bound = asyncio.Event()
    bound_count = 0

    class _Service:
        async def send_message(self, source, *, target_session_id, message, input_mode):
            assert input_mode == "steer"
            captured.append((source, target_session_id, message))
            return {"accepted": True, "status": "queued"}

    toolkit = SessionMessagingToolkit(service=_Service())
    rail = SessionMessagingRouteRail()

    async def invoke(session_id: str, request_id: str, tool_call_id: str) -> None:
        nonlocal bound_count
        route = SessionMessagingRoute(
            session_id=session_id,
            request_id=request_id,
            user_id="user-1",
        )
        run_context = with_session_messaging_route({}, route)["run"]["context"]
        ctx = SimpleNamespace(
            inputs=ToolCallInputs(
                tool_call=SimpleNamespace(id=tool_call_id, name="session_send_message"),
                tool_name="session_send_message",
            ),
            extra={"run_context": run_context},
        )
        await rail.before_tool_call(ctx)
        bound_count += 1
        if bound_count == 2:
            both_bound.set()
        await both_bound.wait()
        try:
            await toolkit.send_message("target-1", request_id)
        finally:
            await rail.after_tool_call(ctx)

    await asyncio.gather(
        invoke("source-1", "request-1", "tool-call-1"),
        invoke("source-2", "request-2", "tool-call-2"),
    )

    by_request = {source.request_id: source for source, _, _ in captured}
    assert by_request["request-1"].session_id == "source-1"
    assert by_request["request-1"].tool_call_id == "tool-call-1"
    assert by_request["request-2"].session_id == "source-2"
    assert by_request["request-2"].tool_call_id == "tool-call-2"


def test_adapter_first_registration_uses_stable_session_tools() -> None:
    class _AbilityManager:
        def __init__(self) -> None:
            self.cards = {}

        def list(self):
            return list(self.cards.values())

        def add(self, card):
            self.cards[card.name] = card

        def remove(self, name):
            self.cards.pop(name, None)

    service = object()
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager())
    adapter._last_mode = "agent.code.normal"
    adapter._session_messaging_toolkit = None
    adapter._session_messaging_route_rail = SessionMessagingRouteRail()
    adapter._register_agent_owned_tool = lambda tool, owner_id: None
    adapter._tool_owner_id = lambda: "test-owner"
    runtime = SimpleNamespace(session_message_service=service)
    runtime_token = set_runtime_context(runtime, SimpleNamespace())
    route_token = bind_session_messaging_route(
        session_id="source-1",
        request_id="request-1",
        user_id="user-1",
    )
    try:
        adapter._ensure_session_messaging_tools_registered("source-1", "web")
    finally:
        reset_session_messaging_route(route_token)
        reset_runtime_context(runtime_token)

    assert set(adapter._instance.ability_manager.cards) == {
        "session_list",
        "session_send_message",
        "session_message_list",
        "session_message_resolve",
        "session_continue_queued",
        "session_read",
    }
    assert not hasattr(adapter._session_messaging_toolkit, "_fallback_route")
    assert adapter._session_messaging_route_rail._service is service


def test_adapter_refreshes_toolkit_service_after_runtime_rebuild() -> None:
    class _AbilityManager:
        def __init__(self) -> None:
            self.cards = {
                name: SimpleNamespace(name=name)
                for name in {
                    "session_list",
                    "session_send_message",
                    "session_message_list",
                    "session_message_resolve",
                    "session_continue_queued",
                    "session_read",
                }
            }

        def list(self):
            return list(self.cards.values())

    old_service = object()
    new_service = object()
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager())
    adapter._last_mode = "agent.code.normal"
    adapter._session_messaging_toolkit = SessionMessagingToolkit(old_service)
    adapter._session_messaging_route_rail = SessionMessagingRouteRail()
    adapter._session_messaging_route_rail.set_service(old_service)
    runtime_token = set_runtime_context(
        SimpleNamespace(session_message_service=new_service), SimpleNamespace()
    )
    try:
        adapter._ensure_session_messaging_tools_registered("source-1", "web")
    finally:
        reset_runtime_context(runtime_token)

    assert adapter._session_messaging_toolkit._service is new_service
    assert adapter._session_messaging_route_rail._service is new_service


def test_code_adapter_mounts_session_messaging_route_rail(monkeypatch) -> None:
    adapter = JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(
        adapter,
        "_instantiate_rails",
        lambda rail_infos, _config_base: rail_infos,
    )

    rail_infos = adapter._build_agent_rails({}, {"models": {}}, mode="code.normal")
    matching = [
        info
        for info in rail_infos
        if info.attr_name == "_session_messaging_route_rail"
    ]

    assert len(matching) == 1
    assert isinstance(matching[0].build_func(), SessionMessagingRouteRail)


def test_gateway_request_cannot_forge_cross_session_origin() -> None:
    request = AgentRequest(
        request_id="external-1",
        params={
            "_jiuwenswarm_cross_session": {"message_id": "forged"},
            "metadata": {
                "_jiuwenswarm_cross_session": {"message_id": "nested-forged"},
            },
        },
        metadata={"_jiuwenswarm_cross_session": {"message_id": "forged"}},
        trusted_session_message_route={"message_id": "forged"},
    )

    _strip_untrusted_session_message_context(request)

    assert "_jiuwenswarm_cross_session" not in request.params
    assert "_jiuwenswarm_cross_session" not in request.params["metadata"]
    assert "_jiuwenswarm_cross_session" not in request.metadata
    assert request.trusted_session_message_route is None


@pytest.mark.asyncio
async def test_interrupt_resume_keeps_mailbox_lineage_out_of_prompt_metadata(
    tmp_path,
) -> None:
    store, service, waiting = await _waiting_mailbox(tmp_path, key="resume-lineage")
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    try:
        state = await server._open_session_message_resume(request)

        assert state is not None
        assert "_jiuwenswarm_cross_session" not in request.params
        assert not request.metadata
        route_context = session_messaging_route_context(request)
        assert route_context == {
            "message_id": waiting.message_id,
            "chain_id": waiting.chain_id,
            "parent_message_id": waiting.parent_message_id,
            "hop_count": waiting.hop_count,
        }

        token = bind_session_messaging_route(
            session_id=request.session_id,
            request_id=request.request_id,
            user_id="user-1",
            cross_session=route_context,
        )
        try:
            inherited = current_session_messaging_route()
            assert inherited is not None
            assert inherited.chain_id == waiting.chain_id
            assert inherited.parent_message_id == waiting.message_id
            assert inherited.hop_count == waiting.hop_count
        finally:
            reset_session_messaging_route(token)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_agentserver_executes_claimed_message_in_target_runtime(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="execute")
    record = store.claim(queued.message_id, "execution-1", "run-1")
    assert record is not None

    captured = {}

    class _Runtime:
        agent_manager = object()

        def stream(self, request, **kwargs):
            captured["request"] = request
            captured["options"] = kwargs

            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.final", "content": "done"},
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = _Runtime()
    server._agent_manager = server._runtime.agent_manager
    pushed = []

    async def send_push(message):
        pushed.append(message)
        return True

    server.send_push = send_push
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_session_metadata",
        lambda *args, **kwargs: {
            "session_id": "target-1",
            "title": "Target",
            "user_id": "user-1",
            "channel_id": "web",
            "mode": "agent.code.normal",
            "project_id": "project-1",
            "project_dir": "/project",
            "work_mode": "code",
        },
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "build_server_push_message",
        lambda **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: None,
    )

    result = await server.execute_internal_session_message(record)

    request = captured["request"]
    assert result.status == "succeeded"
    assert captured["options"]["background"] is True
    assert request.session_id == "target-1"
    assert request.user_id == "user-1"
    assert request.params["project_dir"] == "/project"
    assert request.params["_jiuwenswarm_cross_session"]["message_id"] == (
        record.message_id
    )
    assert "input_mode" not in request.params
    assert [push["payload"]["event_type"] for push in pushed] == [
        "chat.processing_status",
        "chat.final",
        "chat.processing_status",
    ]
    for push in pushed:
        payload = push["payload"]
        assert push["session_id"] == "target-1"
        assert payload["request_id"] == "execution-1"
        assert payload["turn_request_id"] == "execution-1"
        assert payload["message_origin"] == "cross_session_agent"
        assert payload["session_message_id"] == record.message_id
        assert payload["cross_session"]["source_session_id"] == "source-1"
        assert payload["cross_session"]["content"] == "check"
    assert pushed[0]["payload"]["is_processing"] is True
    assert pushed[-1]["payload"]["is_processing"] is False


@pytest.mark.asyncio
async def test_agentserver_persists_question_correlation_before_push(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="execute-question")
    record = store.claim(queued.message_id, "execution-1", "run-1")
    ordering = []

    class _Runtime:
        agent_manager = object()

        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={
                        "event_type": "chat.ask_user_question",
                        "request_id": "tool-call-1",
                        "source": "ask_user_interrupt",
                    },
                    is_complete=True,
                )

            return events()

    class _Service:
        async def mark_waiting(self, message_id, **kwargs):
            ordering.append(("persist", message_id, kwargs))
            return True

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = _Runtime()
    server._agent_manager = server._runtime.agent_manager
    server._session_message_service = _Service()

    async def send_push(message):
        ordering.append(("push", message))
        return True

    server.send_push = send_push
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_session_metadata",
        lambda *a, **k: _metadata("target-1"),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "build_server_push_message",
        lambda **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *a, **k: None,
    )

    result = await server.execute_internal_session_message(record)

    assert result.status == "waiting_user"
    assert ordering[0][0] == "push"
    assert ordering[0][1]["payload"]["event_type"] == "chat.processing_status"
    assert ordering[1] == (
        "persist",
        record.message_id,
        {
            "interrupt_request_id": "tool-call-1",
            "interrupt_source": "ask_user_interrupt",
        },
    )
    assert ordering[2][0] == "push"
    assert ordering[2][1]["payload"]["event_type"] == "chat.ask_user_question"
    assert ordering[2][1]["payload"]["request_id"] == "tool-call-1"
    assert ordering[2][1]["payload"]["turn_request_id"] == "execution-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("history_failure", [False, True])
async def test_failed_session_question_releases_runtime_wait(
    tmp_path, monkeypatch: pytest.MonkeyPatch, history_failure: bool
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="question-failed")
    record = store.claim(queued.message_id, "execution-1", "run-1")

    class _Runtime:
        agent_manager = object()
        release_session_message_interactions = AsyncMock()

        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={
                        "event_type": "chat.ask_user_question",
                        "request_id": "permission-1",
                        "source": "permission_interrupt",
                    },
                    is_complete=True,
                )
            return events()

    runtime = _Runtime()
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = runtime.agent_manager
    server._session_message_service = SimpleNamespace(
        mark_waiting=AsyncMock(return_value=history_failure)
    )
    server.send_push = AsyncMock(return_value=True)
    monkeypatch.setattr(
        agent_ws_server_module, "get_session_metadata",
        lambda *a, **k: _metadata("target-1"),
    )
    monkeypatch.setattr(
        agent_ws_server_module, "build_server_push_message",
        lambda **kwargs: dict(kwargs),
    )
    def complete_history(*args, **kwargs):
        if history_failure:
            raise TimeoutError("history writer timed out")
        return None

    monkeypatch.setattr(
        agent_ws_server_module, "enqueue_history_request_completion", complete_history
    )

    if history_failure:
        with pytest.raises(TimeoutError, match="history writer timed out"):
            await server.execute_internal_session_message(record)
    else:
        result = await server.execute_internal_session_message(record)
        assert result.status == "failed"
    assert [
        call.args[0]["payload"]["event_type"]
        for call in server.send_push.await_args_list
    ] == [
        "chat.processing_status",
        "chat.ask_user_question" if history_failure else "chat.error",
        "chat.processing_status",
    ]
    runtime.release_session_message_interactions.assert_awaited_once_with(
        "target-1", request_id="execution-1"
    )


@pytest.mark.asyncio
async def test_agentserver_failure_after_repeated_question_closes_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=lambda record: asyncio.sleep(0),
        available=False,
    )
    await service.start()
    waiting, _ = _enqueue(store, key="repeated-question-failure")
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="tool-call-first",
        interrupt_source="ask_user_interrupt",
    )
    assert await service.complete_waiting_after_resume(
        "target-1",
        interrupt_request_id="tool-call-first",
        interrupt_source="ask_user_interrupt",
        waiting_user=True,
        failed=False,
        next_interrupt_request_id="tool-call-second",
        next_interrupt_source="permission_interrupt",
    )
    request = AgentRequest(
        request_id="answer-1",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *a, **k: None,
    )

    await server._complete_waiting_session_message_after_external_turn(
        request,
        waiting_user=True,
        waiting_correlation_persisted=True,
        failed=True,
        error="failed after repeated question",
        next_interrupt_request_id="tool-call-second",
        next_interrupt_source="permission_interrupt",
    )

    failed = store.get(waiting.message_id)
    assert failed.status == "failed"
    assert failed.last_error == "failed after repeated question"
    await service.stop()


@pytest.mark.asyncio
async def test_resume_turn_persists_each_repeated_question_correlation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, waiting = await _waiting_mailbox(
        tmp_path, key="multiple-repeated-questions"
    )
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        is_stream=True,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )

    class _Runtime:
        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={
                        "event_type": "chat.ask_user_question",
                        "request_id": "tool-call-second",
                        "source": "permission_interrupt",
                    },
                )
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={
                        "event_type": "chat.ask_user_question",
                        "request_id": "tool-call-third",
                        "source": "ask_user_interrupt",
                    },
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    server._session_stream_tasks = {}
    server._execution_runtime = lambda: _Runtime()
    server._send_runtime_event = lambda *args, **kwargs: asyncio.sleep(
        0, result=True
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: None,
    )

    await server._handle_stream_impl(object(), request, asyncio.Lock())

    rebound = store.waiting_for_resume(
        "target-1", "tool-call-third", "ask_user_interrupt"
    )
    assert rebound is not None
    assert rebound.message_id == waiting.message_id
    await service.stop()


@pytest.mark.asyncio
async def test_stream_delivery_abort_marks_resumed_message_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, waiting = await _waiting_mailbox(
        tmp_path, key="stream-delivery-abort"
    )
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        is_stream=True,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )

    class _Runtime:
        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.final", "content": "done"},
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    server._session_stream_tasks = {}
    server._execution_runtime = lambda: _Runtime()
    server._send_runtime_event = lambda *args, **kwargs: asyncio.sleep(
        0, result=False
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: None,
    )

    await server._handle_stream_impl(object(), request, asyncio.Lock())

    failed_delivery = store.get(waiting.message_id)
    assert failed_delivery.status == "unknown"
    assert failed_delivery.last_error_code == "RESUME_OUTCOME_UNKNOWN"
    await service.stop()


@pytest.mark.asyncio
async def test_unary_disconnect_marks_resumed_message_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, waiting = await _waiting_mailbox(
        tmp_path, key="unary-delivery-abort"
    )
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )

    class _Runtime:
        async def answer_interaction(self, request, **kwargs):
            return [
                RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.final", "content": "done"},
                    is_complete=True,
                )
            ]

    async def disconnect(*args, **kwargs):
        raise agent_ws_server_module.WebSocketConnectionClosed(None, None)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    server._execution_runtime = lambda: _Runtime()
    server._send_runtime_event = disconnect
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(agent_ws_server_module.WebSocketConnectionClosed):
        await server._handle_unary_impl(object(), request, asyncio.Lock())

    failed_delivery = store.get(waiting.message_id)
    assert failed_delivery.status == "unknown"
    assert failed_delivery.last_error_code == "RESUME_OUTCOME_UNKNOWN"
    await service.stop()


@pytest.mark.asyncio
async def test_repeated_question_persistence_failure_marks_message_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, waiting = await _waiting_mailbox(
        tmp_path, key="question-persistence-failure"
    )
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        is_stream=True,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )

    class _Runtime:
        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.ask_user_question"},
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    server._session_stream_tasks = {}
    server._execution_runtime = lambda: _Runtime()
    server._send_runtime_event = lambda *args, **kwargs: asyncio.sleep(
        0, result=True
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="uncorrelated follow-up question"):
        await server._handle_stream_impl(object(), request, asyncio.Lock())

    failed_persistence = store.get(waiting.message_id)
    assert failed_persistence.status == "unknown"
    assert failed_persistence.last_error_code == "RESUME_OUTCOME_UNKNOWN"
    await service.stop()


@pytest.mark.asyncio
async def test_history_barrier_timeout_marks_resumed_message_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, waiting = await _waiting_mailbox(
        tmp_path, key="history-barrier-timeout"
    )
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    runtime = SimpleNamespace(release_session_message_interactions=AsyncMock())
    server._execution_runtime = lambda: runtime
    state = await server._open_session_message_resume(request)
    assert state is not None

    async def timeout_receipt(*args, **kwargs):
        raise TimeoutError("history writer timed out")

    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "wait_for_history_receipt",
        timeout_receipt,
    )

    await server._finalize_session_message_resume(
        request,
        state,
        outcome="succeeded",
        error="",
    )

    timed_out = store.get(waiting.message_id)
    assert timed_out.status == "unknown"
    assert timed_out.last_error_code == "HISTORY_PERSISTENCE_UNCONFIRMED"
    runtime.release_session_message_interactions.assert_awaited_once_with(
        "target-1", request_id=f"session-message-{waiting.message_id}"
    )
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", ["chat.error", "runtime.error", "execution.error", "error"]
)
async def test_agentserver_treats_error_events_as_failed(
    tmp_path, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key=f"execute-{event_type}")
    record = store.claim(queued.message_id, "execution-1", "run-1")

    class _Runtime:
        agent_manager = object()

        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": event_type, "message": "boom"},
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = _Runtime()
    server._agent_manager = server._runtime.agent_manager
    server.send_push = lambda message: asyncio.sleep(0, result=True)
    monkeypatch.setattr(agent_ws_server_module, "get_session_metadata", lambda *a, **k: _metadata("target-1"))
    monkeypatch.setattr(agent_ws_server_module, "build_server_push_message", lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(agent_ws_server_module, "enqueue_history_request_completion", lambda *a, **k: None)

    result = await server.execute_internal_session_message(record)

    assert result.status == "failed"
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_agentserver_does_not_treat_runtime_accepted_as_completion(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="execute-accepted")
    record = store.claim(queued.message_id, "execution-1", "run-1")

    class _Runtime:
        agent_manager = object()

        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "runtime.accepted"},
                )
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload=None,
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = _Runtime()
    server._agent_manager = server._runtime.agent_manager
    server.send_push = lambda message: asyncio.sleep(0, result=True)
    monkeypatch.setattr(agent_ws_server_module, "get_session_metadata", lambda *a, **k: _metadata("target-1"))
    monkeypatch.setattr(agent_ws_server_module, "build_server_push_message", lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(agent_ws_server_module, "enqueue_history_request_completion", lambda *a, **k: None)

    result = await server.execute_internal_session_message(record)

    assert result.status == "unknown"
    assert result.error_code == "EXECUTION_NOT_COMPLETED"


@pytest.mark.asyncio
async def test_agentserver_acceptance_followed_by_final_is_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    queued, _ = _enqueue(store, key="execute-accepted-final")
    record = store.claim(queued.message_id, "execution-1", "run-1")

    class _Runtime:
        agent_manager = object()

        def stream(self, request, **kwargs):
            async def events():
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "runtime.accepted"},
                )
                yield RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.final", "content": "done"},
                    is_complete=True,
                )

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = _Runtime()
    server._agent_manager = server._runtime.agent_manager
    server.send_push = lambda message: asyncio.sleep(0, result=True)
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_session_metadata",
        lambda *a, **k: _metadata("target-1"),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "build_server_push_message",
        lambda **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *a, **k: None,
    )

    result = await server.execute_internal_session_message(record)

    assert result.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_resumed_runtime_accepted_without_final_is_unknown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    store, service, waiting = await _waiting_mailbox(
        tmp_path, key=f"resume-accepted-{streaming}"
    )
    request = AgentRequest(
        request_id="answer-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_ANSWER,
        is_stream=streaming,
        params={
            "request_id": "tool-call-first",
            "source": "ask_user_interrupt",
            "answers": [{"answer": "yes"}],
        },
    )

    accepted = RuntimeEvent(
        request_id=request.request_id,
        channel_id=request.channel_id,
        session_id=request.session_id,
        payload={"event_type": "runtime.accepted"},
        is_complete=True,
    )

    class _Runtime:
        async def answer_interaction(self, request, **kwargs):
            return [accepted]

        def stream(self, request, **kwargs):
            async def events():
                yield accepted

            return events()

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = service
    server._session_stream_tasks = {}
    server._execution_runtime = lambda: _Runtime()
    server._send_runtime_event = lambda *args, **kwargs: asyncio.sleep(
        0, result=True
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "enqueue_history_request_completion",
        lambda *args, **kwargs: None,
    )
    try:
        if streaming:
            await server._handle_stream_impl(object(), request, asyncio.Lock())
        else:
            await server._handle_unary_impl(object(), request, asyncio.Lock())

        record = store.get(waiting.message_id)
        assert record is not None
        assert record.status == "unknown"
        assert record.last_error_code == "RESUME_OUTCOME_UNKNOWN"
        assert "without confirming completion" in record.last_error
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("streaming", [False, True])
async def test_runtime_supersedes_only_after_user_turn_is_admitted_and_accepted(
    accepted: bool,
    streaming: bool,
) -> None:
    admission = SessionRunAdmission()
    observed: list[tuple[str, bool]] = []

    class _Manager:
        async def begin_foreground_chat(self):
            observed.append(("foreground-begin", admission.is_user_active("target-1")))

        async def end_foreground_chat(self):
            observed.append(("foreground-end", admission.is_user_active("target-1")))

    class _PlanController:
        async def ensure_state(self, request, mode, sub_mode, agent):
            return SimpleNamespace(events=[])

        async def check_post_process_exit(self, request, agent):
            return []

    class _Agent:
        @staticmethod
        def _response(request):
            observed.append(("agent", admission.is_user_active("target-1")))
            payload = (
                {"event_type": "chat.final", "content": "done"}
                if accepted
                else {"event_type": "chat.error", "error": "bad request"}
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=accepted,
                payload=payload,
            )

        async def process_message(self, request):
            return self._response(request)

        async def execute_message(self, request):
            return self._response(request)

        async def process_message_stream(self, request):
            yield self._response(request)

    class _Service:
        async def supersede_waiting_for_target(self, session_id):
            observed.append(("supersede", admission.is_user_active(session_id)))
            return 1

    async def initialize() -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=_Manager(),
        initializer=initialize,
        plan_controller=_PlanController(),
        admission_controller=admission,
    )
    runtime.set_session_message_service(_Service())

    async def prepare_chat_turn(request, channel_id, *, sync_metadata):
        return "agent.code.normal", "normal", _Agent()

    runtime._prepare_chat_turn = prepare_chat_turn
    request = AgentRequest(
        request_id="plain-turn-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "query": "hello"},
    )

    if streaming:
        _events = [
            event async for event in runtime.stream(request, trigger_hook=False)
        ]
    else:
        _events = await runtime.invoke(request, trigger_hook=False)

    assert ("agent", True) in observed
    if accepted:
        assert ("supersede", True) in observed
    else:
        assert not any(stage == "supersede" for stage, _active in observed)
    assert admission.is_user_active("target-1") is False


@pytest.mark.asyncio
async def test_rearmed_mailbox_worker_waits_for_active_user(tmp_path) -> None:
    store = SessionMessageStore(tmp_path / "supersede-admission.sqlite3")
    admission = SessionRunAdmission()
    successor_started = asyncio.Event()

    async def execute(record):
        successor_started.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=admission,
        execute=execute,
        available=True,
    )
    await service.start()
    waiting, _ = _enqueue(store, key="supersede-admission-waiting")
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="tool-call-first",
        interrupt_source="ask_user_interrupt",
    )
    successor, _ = _enqueue(
        store,
        key="supersede-admission-next",
        content="next",
    )
    try:
        await admission.begin_user("target-1")
        await service.supersede_waiting_for_target("target-1")
        await asyncio.sleep(0)
        assert not successor_started.is_set()

        await admission.end_user("target-1")
        await asyncio.wait_for(successor_started.wait(), timeout=1)
        await _wait_for_status(store, successor.message_id, "succeeded")
        record = store.get(successor.message_id)
        assert record is not None
        assert record.status == "succeeded"
    finally:
        if admission.is_user_active("target-1"):
            await admission.end_user("target-1")
        await service.stop()


@pytest.mark.asyncio
async def test_plain_user_turn_supersedes_bypassed_waiting_messages(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-resume user turn fails waiting items and re-arms the FIFO."""
    store, service, waiting = await _waiting_mailbox(tmp_path, key="supersede")
    queued, _ = _enqueue(store, key="supersede-next", content="next")
    try:
        assert store.next_queued("target-1") is None

        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._session_message_service = service
        request = AgentRequest(
            request_id="plain-turn-1",
            channel_id="web",
            session_id="target-1",
            req_method=ReqMethod.CHAT_SEND,
            params={"message": "just typing"},
        )

        await runtime._supersede_bypassed_session_messages(request)

        superseded = store.get(waiting.message_id)
        assert superseded is not None
        assert superseded.status == "failed"
        assert superseded.last_error_code == "SUPERSEDED_BY_USER_TURN"
        head = store.next_queued("target-1")
        assert head is not None
        assert head.message_id == queued.message_id
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_interrupt_resume_turn_does_not_supersede_waiting_messages(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering the question through its correlation keeps the item waiting."""
    store, service, waiting = await _waiting_mailbox(tmp_path, key="resume-keep")
    try:
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._session_message_service = service
        request = AgentRequest(
            request_id="resume-turn-1",
            channel_id="web",
            session_id="target-1",
            req_method=ReqMethod.CHAT_SEND,
            params={
                "request_id": "tool-call-first",
                "source": "ask_user_interrupt",
                "answers": [{"value": "yes"}],
            },
        )

        await runtime._supersede_bypassed_session_messages(request)

        record = store.get(waiting.message_id)
        assert record is not None
        assert record.status == "waiting_user"
        assert store.next_queued("target-1") is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_chat_answer_never_supersedes_waiting_messages(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHAT_ANSWER is an interaction answer by definition; never treat it as bypass."""
    store, service, waiting = await _waiting_mailbox(tmp_path, key="answer-keep")
    try:
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime._session_message_service = service
        request = AgentRequest(
            request_id="answer-turn-1",
            channel_id="web",
            session_id="target-1",
            req_method=ReqMethod.CHAT_ANSWER,
            params={"message": "legacy-shaped answer without correlation"},
        )

        await runtime._supersede_bypassed_session_messages(request)

        record = store.get(waiting.message_id)
        assert record is not None
        assert record.status == "waiting_user"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_execution_watchdog_moves_wedged_execution_to_unknown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged execute callback must not hold the target's admission forever."""
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    release = asyncio.Event()
    release_wait = AsyncMock()
    abandoned = asyncio.Event()

    async def on_abandoned(record):
        await release_wait(record)
        abandoned.set()

    async def wedged(record):
        await release.wait()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=wedged,
        on_abandoned_wait=on_abandoned,
        execution_watchdog_timeout=0.05,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        sent = await service.send_message(
            _source("watchdog"), input_mode="follow_up", target_session_id="target-1", message="stuck"
        )
        await _wait_for_status(store, sent["message_id"], "unknown")

        record = store.get(sent["message_id"])
        assert record is not None
        assert record.status == "unknown"
        assert record.last_error_code == "EXECUTION_WATCHDOG_TIMEOUT"
        await asyncio.wait_for(abandoned.wait(), timeout=1)
        release_wait.assert_awaited_once()
        assert release_wait.await_args.args[0].message_id == sent["message_id"]
        admission = service._admission
        assert "target-1" not in admission.active
    finally:
        release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_worker_store_failure_after_question_releases_runtime_wait(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    released = asyncio.Event()
    release_wait = AsyncMock(side_effect=lambda _record: released.set())

    async def execute(record):
        assert await service.mark_waiting(
            record.message_id,
            interrupt_request_id="permission-1",
            interrupt_source="permission_interrupt",
        )
        return SessionMessageExecutionResult(status="waiting_user")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        on_abandoned_wait=release_wait,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    original_store_call = service._store_call

    async def fail_waiting_read(operation, *args, **kwargs):
        if operation == store.get:
            raise sqlite3.OperationalError("database is locked")
        return await original_store_call(operation, *args, **kwargs)

    monkeypatch.setattr(service, "_store_call", fail_waiting_read)
    try:
        sent = await service.send_message(
            _source("failed-waiting-read"),
            input_mode="follow_up", target_session_id="target-1",
            message="ask",
        )
        await _wait_for_status(store, sent["message_id"], "unknown")
        # The durable outcome is visible before the worker's finally callback.
        await asyncio.wait_for(released.wait(), 1)
        release_wait.assert_awaited_once()
        assert release_wait.await_args.args[0].message_id == sent["message_id"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_worker_preserves_confirmed_waiting_question(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    release_wait = AsyncMock()

    async def execute(record):
        assert await service.mark_waiting(
            record.message_id,
            interrupt_request_id="permission-1",
            interrupt_source="permission_interrupt",
        )
        return SessionMessageExecutionResult(status="waiting_user")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=execute,
        on_abandoned_wait=release_wait,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        sent = await service.send_message(
            _source("confirmed-waiting"),
            input_mode="follow_up", target_session_id="target-1",
            message="ask",
        )
        await _wait_for_status(store, sent["message_id"], "waiting_user")
        worker = service._workers.get("target-1")
        if worker is not None:
            await asyncio.wait_for(worker, timeout=1)
        release_wait.assert_not_awaited()
    finally:
        await service.stop()


def test_store_supersede_only_touches_waiting_rows(tmp_path) -> None:
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    waiting, _ = _enqueue(store, key="supersede-waiting")
    store.claim(waiting.message_id, "execution-1", "run-1")
    store.mark_waiting(
        waiting.message_id,
        interrupt_request_id="interrupt-1",
        interrupt_source="ask_user_interrupt",
    )
    finished, _ = _enqueue(store, key="supersede-finished")
    store.claim(finished.message_id, "execution-2", "run-2")
    store.transition_status(
        finished.message_id, "succeeded", expected_statuses=("running",)
    )
    queued, _ = _enqueue(store, key="supersede-queued")

    superseded = store.supersede_waiting_for_target("target-1")

    assert [record.message_id for record in superseded] == [waiting.message_id]
    assert store.get(waiting.message_id).status == "failed"
    assert store.get(finished.message_id).status == "succeeded"
    assert store.get(queued.message_id).status == "queued"

@pytest.mark.asyncio
@pytest.mark.parametrize("protected_mode", ["agent.code.plan", "agent.work.plan", "goal"])
@pytest.mark.parametrize("explicit_steer", [False, True])
async def test_protected_steer_queues_deduplicates_and_survives_restart(
    tmp_path, monkeypatch, protected_mode, explicit_steer,
):
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    admission = SessionRunAdmission()
    protected = True
    admission.set_session_message_blocker(
        lambda sid: sid == "target-1" and protected_mode == "goal" and protected
    )
    await admission.begin_user("target-1")
    executed = []

    async def execute(record):
        executed.append(record)
        return SessionMessageExecutionResult("succeeded")

    def metadata(sid):
        mode = protected_mode if protected and protected_mode != "goal" else "agent.code.normal"
        return {**_metadata(sid), "mode": mode if sid == "target-1" else "agent.code.normal"}

    def make_service():
        result = SessionMessageService(
            store=SessionMessageStore(store.path), admission=admission, execute=execute,
            requires_task_queue=lambda sid: protected_mode == "goal" and protected,
        )
        monkeypatch.setattr(result, "_session_metadata", metadata)
        return result

    service = make_service()
    try:
        first = await service.send_message(
            _source("first"), input_mode="follow_up", target_session_id="target-1", message="first",
        )
        steer = await service.send_message(
            _source("steer"), target_session_id="target-1", message="second",
            **({"input_mode": "steer"} if explicit_steer else {}),
        )
        assert steer["status"] == "queued"
        assert steer["input_mode"] == ""
        assert store.get(steer["message_id"]).requested_input_mode == "steer"
        await service.stop()

        service = make_service()
        await service.start()
        pending = await service.list_messages(_source("list"), target_session_id="target-1")
        assert {row["message_id"] for row in pending["messages"]} == {
            first["message_id"], steer["message_id"],
        }
        assert not executed
        await service.continue_queued_for_target("target-1", "user-1")
        # A suspended plan confirmation / a gap between goal rounds still owns
        # the target after the foreground user turn releases its admission.
        if protected_mode != "goal":
            await admission.mark_interaction_pending("target-1", "plan-confirm")
        await admission.end_user("target-1")
        await asyncio.sleep(0.05)
        assert not executed
        assert store.get(steer["message_id"]).status == "queued"

        protected = False
        await admission.clear_interaction_pending("target-1", "plan-confirm")
        await _wait_for_status(store, steer["message_id"], "succeeded")
        assert [record.content for record in executed] == ["first", "second"]
        assert all(record.input_mode == "" for record in executed)
        duplicate = await service.send_message(
            _source("steer"), target_session_id="target-1", message="second", input_mode="steer",
        )
        assert duplicate["message_id"] == steer["message_id"]
        assert duplicate["deduplicated"] is True
        with pytest.raises(SessionMessagingError) as exc:
            await service.send_message(
                _source("steer"), input_mode="follow_up", target_session_id="target-1", message="second",
            )
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_queued_steer_rechecks_mode_and_retains_fifo(tmp_path, monkeypatch):
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    admission = SessionRunAdmission()
    await admission.begin_user("target-1")
    execute = AsyncMock(return_value=SessionMessageExecutionResult("succeeded"))
    deferred = asyncio.Event()

    async def notify(record):
        if record.requested_input_mode == "steer" and record.input_mode == "":
            deferred.set()

    service = SessionMessageService(
        store=store, admission=admission, execute=execute,
        status_callback=notify, available=False,
    )
    mode = "agent.code.normal"
    monkeypatch.setattr(service, "_session_metadata", lambda sid: {**_metadata(sid), "mode": mode})
    try:
        first = await service.send_message(
            _source("steer"), target_session_id="target-1", message="first", input_mode="steer",
        )
        second = await service.send_message(
            _source("ordinary"), input_mode="follow_up", target_session_id="target-1", message="second",
        )
        assert first["input_mode"] == "steer"
        mode = "agent.code.plan"
        await service.set_available(True)
        await asyncio.wait_for(deferred.wait(), 2)
        execute.assert_not_called()
        await admission.end_user("target-1")
        await _wait_for_status(store, second["message_id"], "succeeded")
        assert [call.args[0].content for call in execute.call_args_list] == ["first", "second"]
        assert store.get(first["message_id"]).input_mode == ""
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_protected_queue_resumes_in_order_without_waiting_for_steer_worker(tmp_path, monkeypatch):
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    execute = AsyncMock(return_value=SessionMessageExecutionResult("succeeded"))
    service = SessionMessageService(
        store=store, admission=_RecordingAdmission(), execute=execute, available=False,
    )
    mode = "agent.code.normal"
    monkeypatch.setattr(service, "_session_metadata", lambda sid: {**_metadata(sid), "mode": mode})
    release_steer = asyncio.Event()
    original_store_call = service._store_call

    async def delay_steer_read(operation, *args, **kwargs):
        result = await original_store_call(operation, *args, **kwargs)
        if operation == store.next_queued and args == ("target-1", "steer") and result:
            await release_steer.wait()
        return result

    monkeypatch.setattr(service, "_store_call", delay_steer_read)
    try:
        first = await service.send_message(
            _source("steer"), target_session_id="target-1", message="first", input_mode="steer",
        )
        second = await service.send_message(
            _source("ordinary"), input_mode="follow_up", target_session_id="target-1", message="second",
        )
        mode = "agent.code.plan"
        await service.set_available(True)
        await _wait_for_status(store, second["message_id"], "succeeded")
        assert [call.args[0].content for call in execute.call_args_list] == ["first", "second"]
        release_steer.set()
        await _wait_for_status(store, first["message_id"], "succeeded")
    finally:
        release_steer.set()
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["cleanup", "persist"])
async def test_stop_during_rejected_steer_deferral_keeps_message_queued(tmp_path, monkeypatch, boundary):
    from jiuwenswarm.runtime.session_input import SessionInputQueueRequiredError

    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    deferring = asyncio.Event()
    release_deferral = asyncio.Event()

    async def wait_for_stop():
        deferring.set()
        await release_deferral.wait()

    async def cleanup(record):
        if boundary == "cleanup":
            await wait_for_stop()

    async def execute(record):
        raise SessionInputQueueRequiredError("target entered plan mode")

    service = SessionMessageService(
        store=store, admission=_RecordingAdmission(), execute=execute,
        on_abandoned_wait=cleanup,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    original_store_call = service._store_call

    async def delay_deferral(operation, *args, **kwargs):
        if operation == store.defer_steering and boundary == "persist":
            await wait_for_stop()
        return await original_store_call(operation, *args, **kwargs)

    monkeypatch.setattr(service, "_store_call", delay_deferral)
    try:
        sent = await service.send_message(
            _source("steer"), target_session_id="target-1", message="task", input_mode="steer",
        )
        await asyncio.wait_for(deferring.wait(), 2)
        stopping = asyncio.create_task(service.stop())
        await asyncio.sleep(0)
        release_deferral.set()
        await asyncio.wait_for(stopping, 2)
        record = store.get(sent["message_id"])
        assert (record.status, record.input_mode) == ("queued", "")
    finally:
        release_deferral.set()
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["exception", "runtime_receipt"])
@pytest.mark.parametrize("reason", ["protected", "waiting_user", "target_changed"])
async def test_steer_rejected_at_delivery_returns_to_task_queue(tmp_path, monkeypatch, refusal, reason):
    from jiuwenswarm.runtime.session_input import (
        SessionInputQueueRequiredError, SessionInputRejectedError, SessionInputTargetError,
    )

    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    admission = SessionRunAdmission()
    await admission.begin_user("target-1")
    deferred = asyncio.Event()
    delivered = []

    async def execute(record):
        if record.input_mode == "steer":
            error = {
                "protected": SessionInputQueueRequiredError,
                "waiting_user": SessionInputRejectedError,
                "target_changed": SessionInputTargetError,
            }[reason]("target temporarily cannot accept input")
            if refusal == "exception":
                raise error
            return SessionMessageExecutionResult("failed", error.code, str(error))
        delivered.append(record)
        return SessionMessageExecutionResult("succeeded")

    async def notify(record):
        if record.requested_input_mode == "steer" and record.input_mode == "":
            deferred.set()

    service = SessionMessageService(
        store=store, admission=admission, execute=execute, status_callback=notify,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        sent = await service.send_message(
            _source("steer"), target_session_id="target-1", message="later task", input_mode="steer",
        )
        await asyncio.wait_for(deferred.wait(), 2)
        record = store.get(sent["message_id"])
        assert (record.status, record.input_mode, record.started_at) == ("queued", "", None)
        assert record.execution_request_id == record.runtime_run_id == ""
        assert not delivered
        await admission.end_user("target-1")
        await _wait_for_status(store, sent["message_id"], "succeeded")
        assert [record.message_id for record in delivered] == [sent["message_id"]]
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_receipt", [False, True])
async def test_deferred_delivery_cleanup_cannot_release_new_task(tmp_path, monkeypatch, cancel_receipt):
    from jiuwenswarm.runtime.session_input import SessionInputQueueRequiredError

    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    started, finish_task, finish_receipt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def execute(record):
        if record.input_mode == "steer":
            raise SessionInputQueueRequiredError("target entered plan mode")
        started.set()
        await finish_task.wait()
        return SessionMessageExecutionResult("succeeded")

    async def cleanup(record):
        cleaned.append(record.input_mode)

    async def notify(record):
        if record.status == "queued" and record.input_mode == "":
            await finish_receipt.wait()

    service = SessionMessageService(
        store=store, admission=_RecordingAdmission(), execute=execute,
        status_callback=notify, on_abandoned_wait=cleanup, available=False,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        sent = await service.send_message(
            _source("steer"), target_session_id="target-1", message="task", input_mode="steer",
        )
        receipt_worker = service._workers[("target-1", "steer")]
        await service.set_available(True)
        await asyncio.wait_for(started.wait(), 2)
        task_worker = service._executing_workers[sent["message_id"]]
        assert cleaned == ["steer"]
        if cancel_receipt:
            receipt_worker.cancel()
        else:
            finish_receipt.set()
        await asyncio.wait_for(asyncio.gather(receipt_worker, return_exceptions=True), 2)
        assert store.get(sent["message_id"]).status == "running"
        assert service._executing_workers[sent["message_id"]] is task_worker
        assert cleaned == ["steer"]
        finish_task.set()
        await _wait_for_status(store, sent["message_id"], "succeeded")
    finally:
        finish_task.set()
        finish_receipt.set()
        await service.stop()


def test_legacy_steer_migration_preserves_idempotency_after_deferral(tmp_path):
    path = tmp_path / "messages.sqlite3"
    store = SessionMessageStore(path)
    kwargs = dict(
        owner_scope_id="user-1", source_session_id="source-1",
        source_title_snapshot="Source", source_request_id="request-1",
        source_tool_call_id="steer", idempotency_key="steer",
        target_session_id="target-1", content="later task", input_mode="steer",
    )
    original, _ = store.enqueue(**kwargs)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE session_messages DROP COLUMN requested_input_mode")
    migrated = SessionMessageStore(path)
    assert migrated.get(original.message_id).requested_input_mode == "steer"
    migrated.defer_steering(original.message_id)
    duplicate, created = migrated.enqueue(**kwargs)
    assert not created
    assert duplicate.message_id == original.message_id
    assert duplicate.sequence == original.sequence
    assert duplicate.input_mode == ""
    assert duplicate.requested_input_mode == "steer"
    assert migrated.claim(
        duplicate.message_id, "stale-steer", "stale-run", expected_input_mode="steer",
    ) is None
    assert migrated.claim(
        duplicate.message_id, "task", "task-run", expected_input_mode="",
    ).input_mode == ""


@pytest.mark.parametrize("blocker", ["waiting_user", "unknown"])
def test_protected_queue_normalization_preserves_steer_barriers(tmp_path, blocker):
    store = SessionMessageStore(tmp_path / "messages.sqlite3")
    kwargs = dict(
        owner_scope_id="user-1", source_session_id="source-1",
        source_title_snapshot="Source", source_request_id="request-1",
        target_session_id="target-1", content="task", input_mode="steer",
    )
    blocked, _ = store.enqueue(**kwargs, source_tool_call_id="blocked", idempotency_key="blocked")
    queued, _ = store.enqueue(**kwargs, source_tool_call_id="queued", idempotency_key="queued")
    store.claim(blocked.message_id, "blocked", "blocked-run")
    store.transition_status(blocked.message_id, blocker, expected_statuses=("running",))
    assert store.next_queued("target-1", defer_steering=True) is None
    assert store.get(queued.message_id).input_mode == "steer"
    store.transition_status(blocked.message_id, "succeeded", expected_statuses=(blocker,))
    ready = store.next_queued("target-1", defer_steering=True)
    assert ready.message_id == queued.message_id
    assert ready.input_mode == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_steer", [False, True])
async def test_steer_bypasses_waiting_task_admission_and_keeps_default_fifo(tmp_path, monkeypatch, explicit_steer):
    from dataclasses import replace

    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    entered, release = asyncio.Event(), asyncio.Event()
    executions = []

    class Admission(_RecordingAdmission):
        async def begin_session_message(self, sid, run):
            entered.set()
            await release.wait()
            await super().begin_session_message(sid, run)

    async def execute(record):
        executions.append(record)
        return SessionMessageExecutionResult('delivered' if record.input_mode else 'succeeded')

    service = SessionMessageService(store=store, admission=Admission(), execute=execute)
    monkeypatch.setattr(service, '_session_metadata', _metadata)
    try:
        ordinary = await service.send_message(_source('ordinary'), input_mode="follow_up", target_session_id='target-1', message='independent task')
        await asyncio.wait_for(entered.wait(), 2)
        source = replace(_source('steer'), chain_id='chain-parent', parent_message_id='parent', hop_count=2)
        steer = await service.send_message(
            source, target_session_id='target-1', message='adjust current task',
            **({'input_mode': 'steer'} if explicit_steer else {}),
        )
        await _wait_for_status(store, steer['message_id'], 'delivered')
        assert store.get(ordinary['message_id']).status == 'queued'
        assert len(executions) == 1
        received = executions[0]
        assert (received.source_session_id, received.source_request_id, received.source_tool_call_id) == ('source-1', 'request-1', 'steer')
        assert (received.chain_id, received.parent_message_id, received.hop_count) == ('chain-parent', 'parent', 3)
        duplicate = await service.send_message(source, target_session_id='target-1', message='adjust current task', input_mode='steer')
        assert duplicate['deduplicated'] and duplicate['status'] == 'delivered'
        with pytest.raises(SessionMessagingError) as exc:
            await service.send_message(source, input_mode="follow_up", target_session_id='target-1', message='adjust current task')
        assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
        release.set()
        await _wait_for_status(store, ordinary['message_id'], 'succeeded')
        assert [r.input_mode for r in executions] == ['steer', '']
        listing = await service.list_messages(source)
        assert next(r for r in listing['messages'] if r['message_id'] == steer['message_id'])['input_mode'] == 'steer'
        assert SessionMessageStore(store.path).get(steer['message_id']).status == 'delivered'
    finally:
        release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_steer_bypasses_running_mailbox_task_and_delete_stops_both_consumers(tmp_path, monkeypatch):
    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    entered = {mode: asyncio.Event() for mode in ('', 'steer')}
    async def execute(record):
        entered[record.input_mode].set()
        await asyncio.Event().wait()
    service = SessionMessageService(store=store, admission=_RecordingAdmission(), execute=execute)
    monkeypatch.setattr(service, '_session_metadata', _metadata)
    try:
        ordinary = await service.send_message(_source('ordinary'), input_mode="follow_up", target_session_id='target-1', message='task')
        await asyncio.wait_for(entered[''].wait(), 2)
        steer = await service.send_message(_source('steer'), target_session_id='target-1', message='adjust', input_mode='steer')
        await asyncio.wait_for(entered['steer'].wait(), 2)
        await service.begin_target_delete('target-1')
        assert store.get(ordinary['message_id']).status == 'unknown'
        assert store.get(steer['message_id']).status == 'unknown'
        assert not service._workers
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_steer_restart_preserves_mode_and_quarantines_uncertain_delivery(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    execute = AsyncMock(return_value=SessionMessageExecutionResult('delivered'))
    service = SessionMessageService(store=store, admission=_RecordingAdmission(), execute=execute, available=False)
    monkeypatch.setattr(service, '_session_metadata', _metadata)
    first = await service.send_message(_source('first'), target_session_id='target-1', message='one', input_mode='steer')
    second = await service.send_message(_source('second'), target_session_id='target-1', message='two', input_mode='steer')
    await service.stop()
    store.claim(first['message_id'], 'lost', 'lost')
    restarted = SessionMessageService(store=SessionMessageStore(store.path), admission=_RecordingAdmission(), execute=execute)
    monkeypatch.setattr(restarted, '_session_metadata', _metadata)
    try:
        await restarted.start()
        assert restarted.store.get(first['message_id']).status == 'unknown'
        assert restarted.store.next_queued('target-1', 'steer') is None
        execute.assert_not_called()
        await restarted.resolve_unknown(_source('resolve'), message_id=first['message_id'], resolution='cancelled')
        await _wait_for_status(store, second['message_id'], 'delivered')
        assert execute.call_args.args[0].input_mode == 'steer'
    finally:
        await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition,expected', [('stream', 'delivered'), ('chat', 'succeeded'), ('ack_only', 'unknown'), ('error', 'failed'), ('unknown_error', 'unknown')])
async def test_cross_session_steer_runtime_receipt_is_not_task_completion(tmp_path, monkeypatch, disposition, expected):
    from unittest.mock import AsyncMock, Mock
    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    service = SessionMessageService(store=store, admission=_RecordingAdmission(), execute=AsyncMock(), available=False)
    monkeypatch.setattr(service, '_session_metadata', _metadata)
    sent = await service.send_message(_source('steer'), target_session_id='target-1', message='adjust current task', input_mode='steer')
    await service.stop()
    record = store.claim(sent['message_id'], 'delivery-id', 'delivery-run')
    class Runtime:
        start = AsyncMock()
        create_or_resume_session = AsyncMock(return_value='target-1')
        async def stream(self, request, **kwargs):
            assert kwargs == {'trigger_hook': False, 'background': False}
            assert request.params['input_mode'] == 'steer'
            origin = request.params[SESSION_MESSAGE_INTERNAL_KEY]
            assert origin['source_session_id'] == 'source-1'
            assert origin['source_tool_call_id'] == 'steer'
            payload = {'event_type': 'runtime.accepted'}
            if disposition == 'stream':
                payload['input_boundary'] = 'stream'
            elif disposition == 'chat':
                payload['input_delivery'] = 'chat'
            elif disposition in {'error', 'unknown_error'}:
                code = 'SESSION_INPUT_DELIVERY_UNKNOWN' if disposition == 'unknown_error' else 'SESSION_INPUT_TARGET_CHANGED'
                payload = {'event_type': 'chat.error', 'code': code, 'error': 'target ended'}
            yield RuntimeEvent(request_id=request.request_id, channel_id='web', session_id='target-1', payload=payload)
            if disposition == 'chat':
                yield RuntimeEvent(request_id=request.request_id, channel_id='web', session_id='target-1', payload={'event_type': 'chat.final', 'content': 'done'}, is_complete=True)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._execution_runtime = lambda: Runtime()
    server.send_push = AsyncMock()
    completion = Mock(return_value=None)
    monkeypatch.setattr(agent_ws_server_module, 'get_session_metadata', lambda *a, **k: _metadata('target-1'))
    monkeypatch.setattr(agent_ws_server_module, 'build_server_push_message', lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(agent_ws_server_module, 'enqueue_history_request_completion', completion)
    result = await server.execute_internal_session_message(record)
    assert result.status == expected
    if disposition != 'chat':
        server.send_push.assert_not_called()
        completion.assert_not_called()
    else:
        completion.assert_called_once()
        statuses = [c.args[0]['payload']['is_processing'] for c in server.send_push.call_args_list if c.args[0]['payload'].get('event_type') == 'chat.processing_status']
        assert statuses == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize('change,error', [('owner', 'NOT_FOUND_OR_FORBIDDEN'), ('hop', 'LIMIT_EXCEEDED'), ('mode', 'INVALID_ARGUMENT')])
async def test_steer_retains_authorization_hop_and_mode_guards(tmp_path, monkeypatch, change, error):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    service = SessionMessageService(store=SessionMessageStore(tmp_path / 'messages.sqlite3'), admission=_RecordingAdmission(), execute=AsyncMock(), available=False)
    monkeypatch.setattr(service, '_session_metadata', lambda sid: {**_metadata(sid), **({'user_id': 'foreign'} if change == 'owner' and sid == 'target-1' else {})})
    try:
        source = replace(_source('steer'), hop_count=4) if change == 'hop' else _source('steer')
        with pytest.raises(SessionMessagingError) as exc:
            await service.send_message(source, target_session_id='target-1', message='adjust', input_mode='invalid' if change == 'mode' else 'steer')
        assert exc.value.code == error
        assert not service.store.list_messages(owner_scope_id='user-1', session_id='source-1')
    finally:
        await service.stop()

@pytest.mark.parametrize('container', ['params', 'metadata', 'nested_metadata'])
def test_cross_session_input_is_never_external_user_authorization(container):
    from jiuwenswarm.server.runtime.agent_adapter.interface import is_external_user_authored_dispatch
    marker = {SESSION_MESSAGE_INTERNAL_KEY: {'message_id': 'sm-agent', 'source_session_id': 'source-1'}}
    params = {'query': 'Agent request', 'input_mode': 'steer'}
    metadata = {}
    if container == 'params':
        params.update(marker)
    elif container == 'metadata':
        metadata.update(marker)
    else:
        params['metadata'] = marker
    assert not is_external_user_authored_dispatch(params, channel_id='web', request_method=ReqMethod.CHAT_SEND, metadata=metadata)

@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', [False, True])
async def test_steer_rejection_and_uncertain_delivery_have_distinct_results(tmp_path, monkeypatch, uncertain):
    from jiuwenswarm.runtime.session_input import SessionInputRejectedError
    from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputDeliveryUnknown
    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    async def execute(record):
        if uncertain:
            raise SessionInputDeliveryUnknown('input may have reached the target')
        if record.input_mode == 'steer':
            raise SessionInputRejectedError('target is waiting for an interaction answer')
        return SessionMessageExecutionResult('succeeded')
    service = SessionMessageService(store=store, admission=_RecordingAdmission(), execute=execute)
    monkeypatch.setattr(service, '_session_metadata', _metadata)
    try:
        sent = await service.send_message(_source('steer'), target_session_id='target-1', message='adjust', input_mode='steer')
        await _wait_for_status(store, sent['message_id'], 'unknown' if uncertain else 'succeeded')
        assert store.get(sent['message_id']).last_error_code == ('SESSION_INPUT_DELIVERY_UNKNOWN' if uncertain else '')
    finally:
        await service.stop()


def test_legacy_mailbox_migration_preserves_default_delivery(tmp_path):
    path = tmp_path / 'messages.sqlite3'
    store = SessionMessageStore(path)
    record, _ = _enqueue(store, key='legacy')
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE session_messages DROP COLUMN input_mode')
    reopened = SessionMessageStore(path)
    assert reopened.get(record.message_id).input_mode == ''
    assert reopened.next_queued('target-1').message_id == record.message_id
    assert reopened.next_queued('target-1', 'steer') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('agent_source', [False, True])
async def test_mailbox_task_does_not_overwrite_incoming_steer_author(tmp_path, monkeypatch, agent_source):
    from unittest.mock import AsyncMock
    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    queued, _ = _enqueue(store, key='original')
    record = store.claim(queued.message_id, 'original-execution', 'run')
    boundary = {'event_type': 'chat.input_received', 'input_request_id': 'new-input', 'content': 'adjust'}
    if agent_source:
        boundary.update(message_origin='cross_session_agent', session_message_id='sm-new',
                        cross_session={'message_id': 'sm-new', 'source_session_id': 'other-source'})
    class Runtime:
        async def stream(self, request, **kwargs):
            yield RuntimeEvent(request_id=request.request_id, channel_id='web', session_id='target-1', payload=boundary)
            yield RuntimeEvent(request_id=request.request_id, channel_id='web', session_id='target-1', payload={'event_type': 'chat.final', 'content': 'done'})
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._execution_runtime = lambda: Runtime()
    server.send_push = AsyncMock()
    monkeypatch.setattr(agent_ws_server_module, 'get_session_metadata', lambda *a, **k: _metadata('target-1'))
    monkeypatch.setattr(agent_ws_server_module, 'build_server_push_message', lambda **kwargs: dict(kwargs))
    monkeypatch.setattr(agent_ws_server_module, 'enqueue_history_request_completion', lambda *a, **k: None)
    result = await server.execute_internal_session_message(record)
    assert result.status == 'succeeded'
    pushed = [call.args[0]['payload'] for call in server.send_push.call_args_list]
    assert next(p for p in pushed if p.get('event_type') == 'chat.input_received') == boundary

@pytest.mark.asyncio
@pytest.mark.parametrize('shutdown', ['stop', 'delete'])
async def test_steer_idle_fallback_remains_owned_during_shutdown(tmp_path, monkeypatch, shutdown):
    store = SessionMessageStore(tmp_path / 'messages.sqlite3')
    started = asyncio.Event()
    async def execute(record):
        service.on_steering_fallback_started(record)
        started.set()
        await asyncio.Event().wait()
    service = SessionMessageService(store=store, admission=_RecordingAdmission(), execute=execute)
    monkeypatch.setattr(service, '_session_metadata', _metadata)
    try:
        sent = await service.send_message(_source('idle-steer'), target_session_id='target-1', message='task', input_mode='steer')
        await asyncio.wait_for(started.wait(), 2)
        if shutdown == 'stop':
            await service.stop()
        else:
            await service.begin_target_delete('target-1')
        assert store.get(sent['message_id']).status == 'unknown'
        assert not service._running_fallbacks
        assert not service._executing_workers
    finally:
        await service.stop()
