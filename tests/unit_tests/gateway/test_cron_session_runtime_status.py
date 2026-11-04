# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron execution remains visible through the registered Web metadata route."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.gateway.cron import scheduler as cron_scheduler_module
from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import CronRunState
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
from jiuwenswarm.gateway.cron.store import CronJobStore
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _TestWebChannel(WebChannel):
    """Expose registered dispatch and real response queues inside the hierarchy."""

    async def metadata(self, session_id: str, user_id: str | None = "alice") -> dict:
        ws_id = "metadata-test"
        ws = SimpleNamespace(_jiuwen_ws_id=ws_id, closed=False)
        queue = asyncio.Queue()
        self._send_queues[ws_id] = queue
        handler = self._method_handlers["session.get_metadata"]
        await handler(ws, "metadata-request", {"session_id": session_id}, "current-web-session", user_id)
        return queue.get_nowait()

    def processing_event(self, session_id: str, busy: bool) -> None:
        self._track_session_busy(Message(
            id="processing-event", type="event", channel_id="web",
            session_id=session_id, params={}, timestamp=0, ok=True,
            payload={"event_type": "chat.processing_status", "is_processing": busy},
        ))


class _TestMessageHandler(MessageHandler):
    """Construct isolated production handlers without modifying its singleton."""

    def __new__(cls, agent_client):
        return object.__new__(cls)

    async def cancel_session(self, session_id, user_id, mode, agent_notify):
        message = Message(
            id="web-stop", type="req", channel_id="web", session_id=session_id,
            params={"intent": "cancel", "mode": mode}, timestamp=0, ok=True,
            req_method=ReqMethod.CHAT_CANCEL, user_id=user_id,
        )
        return await self._cancel_agent_work_for_session(
            message, session_id, agent_notify=agent_notify,
        )

    async def send_interrupt(self, envelope):
        await self._send_interrupt_to_agent(envelope)

    async def drain_cancel_requests(self) -> None:
        await asyncio.gather(*list(self._fire_and_forget_tasks))


class _TestScheduler(CronSchedulerService):
    """Keep protected lifecycle access within a public testing subclass."""

    async def wake(self, job) -> str:
        run_id = f"{job.id}:1793070000"
        self._jobs[job.id] = job
        await self._on_wake(job, run_id)
        return run_id

    def task(self, run_id: str) -> asyncio.Task:
        return self._run_tasks[run_id]

    def state(self, run_id: str) -> CronRunState:
        return self._runs[run_id]

    def remember(self, state: CronRunState, task: asyncio.Task | None = None) -> None:
        self._runs[state.run_id] = state
        if task is not None:
            self._run_tasks[state.run_id] = task

    async def close_runs(self) -> None:
        tasks = list(self._run_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _AgentClient:
    """Control transport completion while production scheduling remains real."""

    server_ready = True

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.stream_error = False
        self.metadata_ok = True
        self.metadata_payload = {"status": "idle", "is_processing": False, "message_count": 2}
        self.requests = []
        self.stream_envelope = None
        self.failure_history_started = asyncio.Event()
        self.failure_history_release = asyncio.Event()
        self.hold_failure_history = False
        self.cancel_started = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.cancel_release.set()
        self.cancel_ok = True
        self.cancel_success = True
        self.cancel_error = False

    async def send_request(self, envelope, timeout=None):
        self.requests.append(envelope)
        method = envelope.method
        if method == ReqMethod.SESSION_CREATE.value:
            payload = {"session_id": "__cron___allocated"}
        elif method == ReqMethod.CHAT_CANCEL.value:
            self.cancel_started.set()
            await self.cancel_release.wait()
            if self.cancel_error:
                raise RuntimeError("controlled interrupt transport failure")
            payload = {"event_type": "chat.interrupt_result", "intent": "cancel"}
            if self.cancel_success is not None:
                payload["success"] = self.cancel_success
            return AgentResponse(
                request_id=envelope.request_id, channel_id=envelope.channel,
                ok=self.cancel_ok, payload=payload,
            )
        elif method == ReqMethod.SESSION_GET_METADATA.value:
            return AgentResponse(
                request_id=envelope.request_id, channel_id=envelope.channel,
                ok=self.metadata_ok, payload=dict(self.metadata_payload),
            )
        else:
            payload = {}
            if self.hold_failure_history and envelope.request_id.startswith("cron-history-"):
                self.failure_history_started.set()
                await self.failure_history_release.wait()
        return AgentResponse(
            request_id=envelope.request_id, channel_id=envelope.channel, ok=True, payload=payload,
        )

    async def send_request_stream(self, envelope):
        self.stream_envelope = envelope
        self.started.set()
        try:
            yield AgentResponseChunk(
                request_id=envelope.request_id, channel_id=envelope.channel,
                payload={"event_type": "chat.delta", "content": "working"},
            )
            await self.release.wait()
            if self.stream_error:
                raise RuntimeError("controlled upstream failure")
            yield AgentResponseChunk(
                request_id=envelope.request_id, channel_id=envelope.channel,
                payload={"event_type": "chat.final", "content": "done"}, is_complete=True,
            )
        finally:
            self.closed.set()


@pytest_asyncio.fixture(name="harness_factory")
async def _harness_factory_fixture(tmp_path):
    harnesses = []

    async def create(mode="agent", user_id="alice", timeout_seconds=None, configured=True):
        store = CronJobStore(path=tmp_path / f"jobs-{len(harnesses)}.json")
        client = _AgentClient()
        handler = _TestMessageHandler(client)
        scheduler = _TestScheduler(store=store, agent_client=client, message_handler=handler)
        controller = CronController(store=store, scheduler=scheduler)
        handler.set_cron_controller(controller)
        channel = _TestWebChannel(WebChannelConfig(), RobotMessageRouter(), agent_client=client)
        _register_web_handlers(WebHandlersBindParams(
            channel=channel, agent_client=client, cron_controller=controller if configured else None,
        ))
        job = await store.create_job(
            name="runtime visibility", cron_expr="0 0 0 * * * *", timezone="UTC",
            description="controlled work", targets="web", mode=mode,
            user_id=user_id, timeout_seconds=timeout_seconds,
        )
        harness = SimpleNamespace(client=client, scheduler=scheduler, controller=controller, handler=handler,
                                  channel=channel, job=job)
        harnesses.append(harness)
        return harness

    yield create
    for harness in harnesses:
        harness.client.cancel_release.set()
        harness.client.failure_history_release.set()
        await harness.handler.drain_cancel_requests()
        await harness.scheduler.close_runs()


async def _start(harness) -> str:
    run_id = await harness.scheduler.wake(harness.job)
    await asyncio.wait_for(harness.client.started.wait(), timeout=2)
    return run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
async def test_cron_execution_visible_to_web_until_natural_completion(harness_factory, mode):
    harness = await harness_factory(mode=mode)
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    assert not harness.channel.is_session_busy(sid)
    for _ in range(3):
        response = await harness.channel.metadata(sid)
        assert response["ok"] is True
        assert response["payload"]["is_processing"] is True
        assert response["payload"]["status"] == "processing"
    harness.client.release.set()
    await asyncio.wait_for(harness.scheduler.task(run_id), timeout=2)
    assert harness.client.closed.is_set()
    harness.client.metadata_payload["status"] = "processing"
    harness.client.metadata_payload["is_processing"] = True
    for _ in range(3):
        response = await harness.channel.metadata(sid)
        assert response["payload"]["is_processing"] is False
        assert response["payload"]["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["timeout", "exception", "cancel"])
async def test_cron_timeout_exception_and_cancel_release_web_status(harness_factory, monkeypatch, end):
    if end == "timeout":
        # Exercise the real wait_for expiry without changing configuration defaults.
        monkeypatch.setattr(cron_scheduler_module, "resolve_cron_job_timeout_seconds", lambda job: 0.1)
    harness = await harness_factory(timeout_seconds=600 if end == "timeout" else None)
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    assert (await harness.channel.metadata(sid))["payload"]["is_processing"] is True
    if end == "exception":
        harness.client.stream_error = True
        harness.client.release.set()
    elif end == "cancel":
        await harness.scheduler.stop_job_runs(harness.job.id)
    await asyncio.wait_for(asyncio.gather(harness.scheduler.task(run_id), return_exceptions=True), timeout=3)
    assert harness.client.closed.is_set()
    assert harness.scheduler.state(run_id).status == "failed"
    response = await harness.channel.metadata(sid)
    assert response["payload"]["is_processing"] is False
    assert response["payload"]["status"] == "idle"
    if end in {"timeout", "cancel"}:
        assert any(request.method == ReqMethod.CHAT_CANCEL.value for request in harness.client.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,query_owner,expected", [
    ("alice", "alice", True), ("alice", "bob", False),
    ("alice", None, False), ("alice", "", False),
    (" alice ", " alice ", True), (" alice ", "alice", True),
    ("", None, True), ("", "", True),
    ("", "alice", False),
])
async def test_cron_visibility_is_scoped_to_exact_owner_and_session(
    harness_factory, owner, query_owner, expected,
):
    harness = await harness_factory(user_id=owner)
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    response = await harness.channel.metadata(sid, user_id=query_owner)
    assert response["payload"]["is_processing"] is expected
    assert harness.controller.is_session_processing(sid, user_id=query_owner) is expected
    other = await harness.channel.metadata(sid + "-other", user_id=query_owner)
    assert other["payload"]["is_processing"] is False
    assert not harness.controller.is_session_processing(sid + "-other", user_id=query_owner)


@pytest.mark.asyncio
async def test_terminal_run_does_not_stay_busy_during_failure_history_write(harness_factory):
    harness = await harness_factory()
    harness.client.hold_failure_history = True
    run_id = await _start(harness)
    harness.client.stream_error = True
    harness.client.release.set()
    await asyncio.wait_for(harness.client.failure_history_started.wait(), timeout=2)
    assert not harness.scheduler.task(run_id).done()
    assert harness.scheduler.state(run_id).status == "failed"
    sid = harness.scheduler.state(run_id).exec_session_id
    assert not harness.controller.is_session_processing(sid, user_id="alice")
    assert (await harness.channel.metadata(sid))["payload"]["is_processing"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status,task_kind,finished", [
    ("running", "done", False), ("running", "missing", False),
    ("pending", "missing", False), ("running", "live", True),
])
async def test_stale_or_future_runs_are_not_processing(harness_factory, status, task_kind, finished):
    harness = await harness_factory()
    now = datetime.now(timezone.utc).isoformat()
    state = CronRunState(run_id="queued-or-stale", job_id=harness.job.id,
                         wake_at_iso=now, push_at_iso=now, status=status,
                         exec_session_id="__cron___not_active", exec_user_id="alice")
    if finished:
        state.finished_at = 1793070000.0
    task = None
    if task_kind == "done":
        task = asyncio.create_task(asyncio.sleep(0))
        await task
    elif task_kind == "live":
        task = asyncio.create_task(asyncio.Event().wait())
    harness.scheduler.remember(state, task)
    assert not harness.controller.is_session_processing(state.exec_session_id, user_id="alice")
    response = await harness.channel.metadata(state.exec_session_id)
    assert response["payload"]["is_processing"] is False
    assert response["payload"]["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_normal_web_busy_state_survives_with_or_without_cron_controller(harness_factory, configured):
    harness = await harness_factory(configured=configured)
    harness.channel.processing_event("web-session", True)
    response = await harness.channel.metadata("web-session")
    assert response["payload"]["is_processing"] is True
    assert response["payload"]["status"] == "processing"
    harness.channel.processing_event("web-session", False)
    response = await harness.channel.metadata("web-session")
    assert response["payload"]["is_processing"] is False
    assert response["payload"]["status"] == "idle"


@pytest.mark.asyncio
async def test_failed_metadata_keeps_upstream_error_without_runtime_overlay(harness_factory):
    harness = await harness_factory()
    run_id = await _start(harness)
    harness.client.metadata_ok = False
    harness.client.metadata_payload = {"code": "NOT_FOUND", "error": "session missing"}
    response = await harness.channel.metadata(harness.scheduler.state(run_id).exec_session_id)
    assert response["ok"] is False
    assert response["code"] == "NOT_FOUND"
    assert response["error"] == "session missing"
    assert response["payload"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
@pytest.mark.parametrize("agent_notify", ["await", "fire_and_forget"])
async def test_confirmed_web_cancel_finishes_cron_waiter_without_waiting_for_eof(
    harness_factory, mode, agent_notify,
):
    harness = await harness_factory(mode=mode)
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    harness.client.cancel_release.clear()
    caller = asyncio.create_task(harness.handler.cancel_session(sid, "alice", mode, agent_notify))
    try:
        await asyncio.wait_for(harness.client.cancel_started.wait(), timeout=2)
        assert not harness.scheduler.task(run_id).done()
        assert not harness.client.closed.is_set()
        assert (await harness.channel.metadata(sid))["payload"]["is_processing"] is True
        harness.client.cancel_release.set()
        assert await asyncio.wait_for(caller, timeout=2) is True
        await asyncio.wait_for(harness.handler.drain_cancel_requests(), timeout=2)
        assert harness.scheduler.task(run_id).done()
        assert harness.client.closed.is_set()
        assert not harness.client.release.is_set()
        assert harness.scheduler.state(run_id).finished_at is not None
        for _ in range(3):
            response = await harness.channel.metadata(sid)
            assert response["payload"]["status"] == "idle"
            assert response["payload"]["is_processing"] is False
        cancels = [request for request in harness.client.requests if request.method == ReqMethod.CHAT_CANCEL.value]
        assert len(cancels) == 1
        assert cancels[0].session_id == sid
        assert cancels[0].user_id == "alice"
    finally:
        harness.client.cancel_release.set()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_notify", ["await", "fire_and_forget"])
@pytest.mark.parametrize("failure", ["response_error", "rejected", "missing_success", "transport_error"])
async def test_unsuccessful_interrupt_ack_keeps_cron_execution_visible(
    harness_factory, agent_notify, failure,
):
    harness = await harness_factory()
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    if failure == "response_error":
        harness.client.cancel_ok = False
        harness.client.cancel_success = False
    elif failure == "rejected":
        harness.client.cancel_success = False
    elif failure == "missing_success":
        harness.client.cancel_success = None
    else:
        harness.client.cancel_error = True
    await harness.handler.cancel_session(sid, "alice", "agent", agent_notify)
    await asyncio.wait_for(harness.handler.drain_cancel_requests(), timeout=2)
    assert harness.client.cancel_started.is_set()
    assert not harness.scheduler.task(run_id).done()
    assert harness.scheduler.state(run_id).status == "running"
    assert not harness.client.closed.is_set()
    response = await harness.channel.metadata(sid)
    assert response["payload"]["status"] == "processing"
    assert response["payload"]["is_processing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_notify", ["await", "fire_and_forget"])
@pytest.mark.parametrize("query_owner,query_session", [
    ("bob", "__cron___allocated"), (None, "__cron___allocated"),
    ("", "__cron___allocated"), ("alice", "__cron___allocated-other"),
    ("alice", "ordinary-web-session"),
])
async def test_confirmed_cancel_does_not_finish_other_owner_or_session_runs(
    harness_factory, agent_notify, query_owner, query_session,
):
    harness = await harness_factory()
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    assert await harness.handler.cancel_session(query_session, query_owner, "agent", agent_notify) is True
    await asyncio.wait_for(harness.handler.drain_cancel_requests(), timeout=2)
    assert not harness.scheduler.task(run_id).done()
    assert not harness.client.closed.is_set()
    response = await harness.channel.metadata(sid, user_id="alice")
    assert response["payload"]["is_processing"] is True
    assert response["payload"]["status"] == "processing"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["pause", "resume", "supplement"])
async def test_non_cancel_interrupt_does_not_finish_cron_waiter(harness_factory, intent):
    harness = await harness_factory()
    run_id = await _start(harness)
    sid = harness.scheduler.state(run_id).exec_session_id
    envelope = e2a_from_agent_fields(
        request_id="non-cancel-interrupt", channel_id="web", session_id=sid,
        req_method=ReqMethod.CHAT_CANCEL, params={"intent": intent},
        user_id="alice", is_stream=False, timestamp=0,
    )
    await harness.handler.send_interrupt(envelope)
    assert not harness.scheduler.task(run_id).done()
    assert not harness.client.closed.is_set()
    assert (await harness.channel.metadata(sid))["payload"]["is_processing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["alice", None])
async def test_team_timeout_ack_keeps_current_run_alive_until_timeout_result_is_saved(
    harness_factory, monkeypatch, owner,
):
    monkeypatch.setattr(cron_scheduler_module, "resolve_cron_job_timeout_seconds", lambda job: 0.1)
    harness = await harness_factory(mode="team", timeout_seconds=600, user_id=owner)
    run_id = await _start(harness)
    task = harness.scheduler.task(run_id)
    response = await harness.channel.metadata(harness.scheduler.state(run_id).exec_session_id, user_id=owner)
    assert response["payload"]["is_processing"] is True
    assert response["payload"]["status"] == "processing"
    await asyncio.wait_for(task, timeout=2)
    state = harness.scheduler.state(run_id)
    assert not task.cancelled()
    assert state.status == "failed"
    assert state.error != "cancelled"
    assert "1min" in state.result_text
    assert state.finished_at is not None
    assert harness.client.closed.is_set()
    assert harness.client.cancel_started.is_set()
    response = await harness.channel.metadata(state.exec_session_id, user_id=owner)
    assert response["payload"]["is_processing"] is False
    assert response["payload"]["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_stopped_owner_metadata_is_idle_while_child_keeps_git_guard_busy(harness_factory, stopped):
    harness = await harness_factory(mode="team")
    sid = "owner-with-running-child"
    harness.client.metadata_payload["organization_owner_stopped"] = stopped
    harness.channel.processing_event(sid, True)
    harness.channel._track_session_busy(Message(
        id="child-start", type="event", channel_id="web", session_id=sid,
        params={}, timestamp=0, ok=True,
        payload={"event_type": "chat.processing_status", "is_processing": True,
                 "source": "org_expert_background", "team_id": "child", "request_id": "child-round"},
    ))
    response = await harness.channel.metadata(sid)
    assert response["payload"]["is_processing"] is not stopped
    assert response["payload"]["status"] == ("idle" if stopped else "processing")
    assert harness.channel.is_session_busy(sid)
    assert ("child", "child-round") in harness.channel._expert_processing_requests[sid]
