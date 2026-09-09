# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durability, ordering, isolation, and admission tests for Session messaging."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

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
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.request import sync_chat_request_metadata
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.service import AgentRuntime
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

    SessionMessageStore(path).ensure_schema()

    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(session_messages)")
        }
    assert {"interrupt_request_id", "interrupt_source"} <= columns


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
        _source("call-1"), target_session_id="target-1", message="first"
    )
    await asyncio.sleep(0)
    assert executed == []
    assert store.get(first["message_id"]).status == "queued"

    await service.set_available(True)
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = await service.send_message(
        _source("call-2"), target_session_id="target-1", message="second"
    )
    await asyncio.sleep(0)
    assert executed == ["first"]

    allow_first.set()
    await asyncio.wait_for(all_finished.wait(), timeout=1)
    for _ in range(20):
        if store.get(second["message_id"]).status == "succeeded":
            break
        await asyncio.sleep(0)

    assert executed == ["first", "second"]
    assert store.get(first["message_id"]).status == "succeeded"
    assert store.get(second["message_id"]).status == "succeeded"
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
        _source("uncertain-1"), target_session_id="target-1", message="first"
    )
    second = await service.send_message(
        _source("uncertain-2"), target_session_id="target-1", message="second"
    )
    await asyncio.wait_for(attempted.wait(), timeout=1)
    for _ in range(20):
        if store.get(first["message_id"]).status == "unknown":
            break
        await asyncio.sleep(0)

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
        _source("ack-only-1"), target_session_id="target-1", message="first"
    )
    second = await service.send_message(
        _source("ack-only-2"), target_session_id="target-1", message="second"
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
        _source("delete-1"), target_session_id="target-1", message="running"
    )
    queued = await service.send_message(
        _source("delete-2"), target_session_id="target-1", message="queued"
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
        _source("delete-abort"), target_session_id="target-1", message="queued"
    )
    await service.begin_target_delete("target-1")
    with pytest.raises(SessionMessagingError) as exc_info:
        await service.send_message(
            _source("delete-blocked"),
            target_session_id="target-1",
            message="too late",
        )
    assert exc_info.value.code == "NOT_FOUND_OR_FORBIDDEN"
    await service.set_available(True)
    await asyncio.sleep(0)
    assert not executed.is_set()
    assert store.get(sent["message_id"]).status == "queued"

    await service.abort_target_delete("target-1")
    await asyncio.wait_for(executed.wait(), timeout=1)
    for _ in range(20):
        if store.get(sent["message_id"]).status == "succeeded":
            break
        await asyncio.sleep(0)

    assert store.get(sent["message_id"]).status == "succeeded"
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_ok", [True, False])
async def test_agentserver_pauses_mailbox_around_online_delete(delete_ok: bool) -> None:
    ordering = []

    class _Service:
        async def begin_target_delete(self, session_id):
            ordering.append(("pause", session_id))

        async def on_target_deleted(self, session_id):
            ordering.append(("commit", session_id))

        async def abort_target_delete(self, session_id):
            ordering.append(("abort", session_id))

    class _Runtime:
        async def delete_session(self, **kwargs):
            ordering.append(("delete", kwargs["session_id"]))
            return SimpleNamespace(
                ok=delete_ok,
                session_id=kwargs["session_id"],
                error_message="delete failed",
                error_code="DELETE_FAILED",
            )

    class _WebSocket:
        async def send(self, payload):
            ordering.append(("reply", payload))

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = _Service()
    server._execution_runtime = lambda: _Runtime()
    request = AgentRequest(
        request_id="delete-1",
        channel_id="web",
        session_id="target-1",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "target-1"},
    )

    await server._handle_session_delete(_WebSocket(), request, asyncio.Lock())

    expected_resolution = "commit" if delete_ok else "abort"
    assert ordering[0:3] == [
        ("pause", "target-1"),
        ("delete", "target-1"),
        (expected_resolution, "target-1"),
    ]
    assert ordering[3][0] == "reply"


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
            target_session_id="other-user",
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
            target_session_id="owned-target",
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
        target_session_id="target-1",
        message="retry me",
    )

    await asyncio.wait_for(executed.wait(), timeout=1)
    for _ in range(20):
        if store.get(sent["message_id"]).status == "succeeded":
            break
        await asyncio.sleep(0)

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
        target_session_id="target-1",
        message="retry on the next loop",
    )

    await asyncio.wait_for(executed.wait(), timeout=1)
    for _ in range(20):
        if store.get(sent["message_id"]).status == "succeeded":
            break
        await asyncio.sleep(0)

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
        _source("waiting-race"), target_session_id="target-1", message="ask"
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
    for _ in range(20):
        if not service._workers:
            break
        await asyncio.sleep(0)

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
        target_session_id="target-1",
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
        async def send_message(self, source, *, target_session_id, message):
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
    }
    assert not hasattr(adapter._session_messaging_toolkit, "_fallback_route")


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
    runtime_token = set_runtime_context(
        SimpleNamespace(session_message_service=new_service), SimpleNamespace()
    )
    try:
        adapter._ensure_session_messaging_tools_registered("source-1", "web")
    finally:
        reset_runtime_context(runtime_token)

    assert adapter._session_messaging_toolkit._service is new_service


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
    assert pushed[0]["session_id"] == "target-1"


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
    assert ordering[0] == (
        "persist",
        record.message_id,
        {
            "interrupt_request_id": "tool-call-1",
            "interrupt_source": "ask_user_interrupt",
        },
    )
    assert ordering[1][0] == "push"


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

    runtime.prepare_chat_turn = prepare_chat_turn
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
        for _ in range(20):
            record = store.get(successor.message_id)
            if record is not None and record.status == "succeeded":
                break
            await asyncio.sleep(0)
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

    async def wedged(record):
        await release.wait()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=store,
        admission=_RecordingAdmission(),
        execute=wedged,
        execution_watchdog_timeout=0.05,
        available=True,
    )
    monkeypatch.setattr(service, "_session_metadata", _metadata)
    try:
        sent = await service.send_message(
            _source("watchdog"), target_session_id="target-1", message="stuck"
        )
        for _ in range(200):
            record = store.get(sent["message_id"])
            if record is not None and record.status == "unknown":
                break
            await asyncio.sleep(0.02)

        assert record is not None
        assert record.status == "unknown"
        assert record.last_error_code == "EXECUTION_WATCHDOG_TIMEOUT"
        admission = service._admission
        assert "target-1" not in admission.active
    finally:
        release.set()
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
