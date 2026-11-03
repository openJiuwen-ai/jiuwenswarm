"""Request-owned cron cleanup must preserve newer work in the same Session."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.runtime.session.model import CancelExecutionResult, SessionExecutionState
from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import SessionRunAdmission
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.handlers.chat import handle_chat_cancel


def _runtime(coordinator):
    runtime = object.__new__(AgentRuntime)
    runtime._closed = False
    runtime._session_coordinator = coordinator
    runtime._admission_controller = SessionRunAdmission()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["cron-finished", "cron-next"])
async def test_runtime_late_or_mismatched_cron_cancel_preserves_next_execution(monkeypatch, target):
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session("session", "web")
    await coordinator.run_unary("session", "cron-finished", SessionWorkKind.CHAT_UNARY, lambda: asyncio.sleep(0))
    started = asyncio.Event()
    release = asyncio.Event()

    async def operation():
        started.set()
        await release.wait()
        return "next reply"

    next_task = asyncio.create_task(coordinator.run_unary("session", "cron-next", SessionWorkKind.CHAT_UNARY, operation))
    await asyncio.wait_for(started.wait(), 1)
    runtime = _runtime(coordinator)
    await runtime._mark_pending_interaction_id("session", "next-control")
    request = AgentRequest(
        request_id="cron-cancel", channel_id="web", session_id="session", req_method=ReqMethod.CHAT_CANCEL,
        params={"mode": "agent", "target_request_id": target, "expected_execution_id": "obsolete-execution"},
    )
    try:
        response = await runtime.cancel_request(request)
        assert response.payload["matched"] == 0
        assert response.payload["stop_confirmed"] is False
        assert runtime._admission_controller.has_pending_interaction("session")
        assert not next_task.done()
        release.set()
        assert await asyncio.wait_for(next_task, 1) == "next reply"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_runtime_cron_cancel_stops_target_and_allows_queued_followup(monkeypatch):
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session("session", "web")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def first_operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    first = asyncio.create_task(coordinator.run_unary("session", "cron-first", SessionWorkKind.CHAT_UNARY, first_operation))
    await asyncio.wait_for(started.wait(), 1)
    followup = asyncio.create_task(coordinator.run_unary(
        "session", "followup", SessionWorkKind.CHAT_UNARY, lambda: asyncio.sleep(0, result="followup"),
    ))
    runtime = _runtime(coordinator)
    assert coordinator.record_interaction("session", "cron-first", frozenset({"target-control"}))
    await runtime._mark_pending_interaction_id("session", "target-control")
    await runtime._mark_pending_interaction_id("session", "unrelated-control")
    try:
        sink = SimpleNamespace(send_response=AsyncMock())
        await handle_chat_cancel(SimpleNamespace(
            request=AgentRequest(
                request_id="cancel-first", channel_id="web", session_id="session", req_method=ReqMethod.CHAT_CANCEL,
                params={"mode": "agent", "target_request_id": "cron-first"},
            ),
            sink=sink,
            services=SimpleNamespace(
                execution_runtime=lambda: runtime,
                session_stream_tasks={"session": {followup: asyncio.Event()}},
            ),
        ))
        response = sink.send_response.await_args.args[0]
        assert response.payload["matched"] == 1
        assert response.payload["stop_confirmed"] is True
        assert runtime._admission_controller.has_pending_interaction("session")
        await runtime._clear_pending_interaction("session", "unrelated-control")
        assert not runtime._admission_controller.has_pending_interaction("session")
        assert stopped.is_set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.wait_for(followup, 1) == "followup"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_targeted_cancel_stops_control_child_after_root_response_ended(monkeypatch):
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session("session", "web")
    record = coordinator._sessions["session"]
    parent = coordinator._new_execution(record, "cron-first", SessionWorkKind.CHAT_STREAM)
    child = coordinator._new_execution(
        record, "control-response", SessionWorkKind.CONTROL_INPUT, parent_execution_id=parent.execution_id,
    )
    coordinator._registry.mark_terminal(parent, SessionExecutionState.SUCCEEDED)
    coordinator._registry.mark_running(child)
    producer = asyncio.create_task(asyncio.Event().wait())
    child.task = producer
    runtime = _runtime(coordinator)
    try:
        request = AgentRequest(
            request_id="cancel", channel_id="web", session_id="session", req_method=ReqMethod.CHAT_CANCEL,
            params={"mode": "agent", "target_request_id": "cron-first", "expected_execution_id": parent.execution_id},
        )
        response = await runtime.cancel_request(request)
        assert response.payload["stop_confirmed"] is True
        assert response.payload["matched"] == 1
        assert producer.cancelled()
        assert child.state is SessionExecutionState.CANCELLED
        response = await runtime.cancel_request(request)
        assert response.payload["matched"] == 0
        assert response.payload["stop_confirmed"] is True
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_team_late_cron_completion_does_not_remove_new_producer():
    manager = object.__new__(TeamManager)
    manager._session_locks = {}
    previous = {"request_id": "cron-old"}
    current = {"request_id": "new-request"}
    producer = object()
    manager._cron_team_completion = {"session": current}
    manager._stream_tasks = {"session": producer}
    manager.broadcast_event = AsyncMock()
    await manager.finish_cron_stream("session", previous, "old-round")
    assert manager._stream_tasks["session"] is producer
    assert manager._cron_team_completion["session"] is current
    manager.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_waits_for_cron_producer_teardown_before_admission(monkeypatch):
    from dataclasses import dataclass

    manager = TeamManager()
    stopping = asyncio.Event()
    finish_cleanup = asyncio.Event()
    entered = asyncio.Event()

    async def producer():
        try:
            await asyncio.Event().wait()
        finally:
            async with manager._get_lifecycle_lock("session"):
                stopping.set()
            await finish_cleanup.wait()

    task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    manager._stream_tasks["session"] = task
    completion = {"request_id": "cron-first", "stream_task": task}
    manager._cron_team_completion["session"] = completion
    manager.broadcast_event = AsyncMock()
    finisher = asyncio.create_task(manager.finish_cron_stream("session", completion, 7))
    await stopping.wait()

    @dataclass
    class Request:
        channel_id: str = "web"
        session_id: str = "session"
        request_id: str = "followup"
        metadata: object = None

    async def next_round(request, inputs, deep_agent, *, team_manager, startup, execution_context):
        team_manager.begin_round(request.session_id, request.request_id)
        entered.set()
        if False:
            yield None

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    monkeypatch.setattr(team_helpers, "_process_team_message_stream", next_round)

    async def consume():
        async for _ in team_helpers._process_team_message_stream_locked(Request(), {}, None):
            pass

    followup = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set()
        assert "followup" not in manager._inflight_requests.get("session", set())
        finish_cleanup.set()
        await asyncio.wait_for(asyncio.gather(finisher, followup), 1)
        assert entered.is_set()
        assert not manager._request_stop_events
        assert manager.get_cron_completion("session") is None
        manager.broadcast_event.assert_awaited_once()
    finally:
        finish_cleanup.set()
        for pending in (task, finisher, followup):
            pending.cancel()
        await asyncio.gather(task, finisher, followup, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_cancel_timeout_still_clears_cancelled_interactions(monkeypatch):
    def _execution(execution_id: str, control_id: str):
        return SimpleNamespace(
            execution_id=execution_id,
            request_id="cron-first",
            root_request_id=None,
            parent_execution_id=None,
            state=SimpleNamespace(terminal=False),
            waiting_control_ids=frozenset({control_id}),
        )

    cancelled_execution = _execution("exec-cancelled", "cancelled-control")
    timed_out_execution = _execution("exec-timeout", "timeout-control")

    async def cancel_execution(session_id: str, *, execution_id: str, generation: int):
        if execution_id == cancelled_execution.execution_id:
            return CancelExecutionResult(matched=1, cancelled=1)
        return CancelExecutionResult(
            matched=1,
            cancelled=0,
            timed_out=(timed_out_execution.execution_id,),
        )

    coordinator = SimpleNamespace(
        snapshot_session=lambda session_id: SimpleNamespace(
            session_id=session_id,
            generation=1,
            executions=(cancelled_execution, timed_out_execution),
        ),
        cancel_execution=cancel_execution,
    )
    runtime = _runtime(coordinator)
    await runtime._mark_pending_interaction_id("session", "cancelled-control")
    await runtime._mark_pending_interaction_id("session", "timeout-control")

    response = await runtime.cancel_request(AgentRequest(
        request_id="cancel-cron", channel_id="web", session_id="session", req_method=ReqMethod.CHAT_CANCEL,
        params={"mode": "agent", "target_request_id": "cron-first"},
    ))

    assert response.payload["matched"] == 2
    # One target timed out, so the stop overall is not confirmed.
    assert response.payload["stop_confirmed"] is False
    # The cancelled target's prompt is gone; only the live one keeps its prompt.
    assert runtime._admission_controller.has_pending_interaction("session")
    await runtime._clear_pending_interaction("session", "timeout-control")
    assert not runtime._admission_controller.has_pending_interaction("session")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["followup", "cron-first"])
async def test_team_targeted_stop_preserves_inflight_followup(owner):
    manager = TeamManager()
    manager._active_rounds["session"] = SimpleNamespace(request_id=owner)
    manager._inflight_requests["session"] = {owner, "followup"}
    assert not await manager.stop_session_runtime("session", expected_request_id="cron-first")
    assert manager._inflight_requests["session"] == {owner, "followup"}


@pytest.mark.asyncio
async def test_team_scheduled_finisher_survives_consumer_cleanup(monkeypatch):
    manager = TeamManager()
    producer = asyncio.create_task(asyncio.Event().wait())
    manager._stream_tasks["session"] = producer
    queue = asyncio.Queue()
    manager.add_waiter("session", "cron-first", queue)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    events = [
        {"event_type": "workflow.updated", "workflow": {"status": "completed"}},
        {"event_type": "chat.final", "content": "Final report", "rid": 7},
    ]
    try:
        async with manager._get_lifecycle_lock("session"):
            for event in events:
                team_helpers._try_finish_cron_team_stream("web", "session", event)
                queue.put_nowait(event)
            completion = manager.get_cron_completion("session")
            assert completion["finish_scheduled"]
            async for _ in team_helpers._wait_for_bounded_team_round_events(
                request_queue=queue, round_state=team_helpers.new_cron_team_round_state(),
                request_id="cron-first", channel_id="web", session_id="session",
            ):
                pass
            manager.remove_waiter("session", "cron-first")
            team_helpers._release_cron_waiter_completion(manager, "session", "cron-first")
            assert manager.get_cron_completion("session") is completion
        await asyncio.wait_for(asyncio.gather(producer, return_exceptions=True), 1)
        # Let the finisher finish its terminal broadcast as well as cancel the producer.
        for _ in range(10):
            if manager.get_cron_completion("session") is None:
                break
            await asyncio.sleep(0)
        assert producer.cancelled()
        assert not manager.has_stream_task("session")
        assert manager.get_cron_completion("session") is None
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_targeted_cancel_checks_scope_and_reports_request_done(monkeypatch):
    """A cancelled dispatch must still release the Front request waiter."""
    from jiuwenswarm.server.ipc.protocol import KIND_REQUEST_DONE
    from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
    from jiuwenswarm.server.worker.event_store import WorkerEventStore

    sent: list[dict] = []

    class _Conn:
        async def send(self, payload: dict) -> None:
            sent.append(payload)

    class _Runtime:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def dispatch_parsed_request(self, bridge, request, lock):
            self.started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.server.worker.dispatcher.activate_external_cli_runtime_paths",
        lambda: None,
    )
    events = WorkerEventStore()
    dispatcher = WorkerDispatcher(SimpleNamespace(
        events=events,
        observe_execution_entry=lambda *args, **kwargs: None,
        holder=SimpleNamespace(generation=1, conn=_Conn()),
    ))
    runtime = _Runtime()
    dispatcher._runtime = runtime
    dispatcher._ready = True
    request = AgentRequest(
        request_id="cron-first", channel_id="web", session_id="session",
        req_method=ReqMethod.CHAT_SEND, params={"mode": "agent"},
    )
    task = asyncio.create_task(dispatcher._run_request(request, 1))
    await asyncio.wait_for(runtime.started.wait(), 1)
    cancel = AgentRequest(
        request_id="cancel", channel_id="web", session_id="session",
        req_method=ReqMethod.CHAT_CANCEL, params={"target_request_id": "cron-first"},
    )
    monkeypatch.setattr(dispatcher, "_reconcile_execution_state", lambda request: SimpleNamespace(
        executions=[SimpleNamespace(request_id="cron-first", root_request_id="cron-first")],
    ))
    await dispatcher._cancel_target_dispatch(cancel)
    assert not task.done()
    monkeypatch.setattr(dispatcher, "_reconcile_execution_state", lambda request: None)
    cancel.metadata = {"app_id": "other"}
    await dispatcher._cancel_target_dispatch(cancel)
    assert not task.done()
    cancel.metadata = None
    await dispatcher._cancel_target_dispatch(cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    done = [item for item in sent if item.get("kind") == KIND_REQUEST_DONE]
    assert done and done[0]["request_id"] == "cron-first"
    assert done[0]["dispatch_id"]
