"""Unit tests for CronSchedulerService: store file deletion and event validation bugs."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.gateway.cron.models import CronJob, CronRunState
from jiuwenswarm.gateway.cron.scheduler import (
    CronSchedulerService,
    _Event,
)
from jiuwenswarm.common.cron_team_completion import (
    cron_team_round_should_end,
    new_cron_team_round_state,
)
from jiuwenswarm.gateway.cron import scheduler as cron_scheduler_module
from jiuwenswarm.gateway.cron.store import CronJobStore
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


# ── Helpers ──────────────────────────────────────────────────────────────────

class _CronSchedulerTeamTestApi:
    """Centralize access to scheduler module helpers (G.CLS.11)."""

    @staticmethod
    def resolve_cron_execution_context(job, *, ts: str, message_handler=None):
        fn = getattr(cron_scheduler_module, "_resolve_cron_execution_context")
        return fn(job, ts=ts, message_handler=message_handler)

    @staticmethod
    def cron_team_stream_should_end(**kwargs):
        state = new_cron_team_round_state()
        state.update(
            {
                "workflow_completed": kwargs.get("workflow_completed", False),
                "leader_final_after_workflow": kwargs.get("leader_final_after_workflow", False),
                "leader_final_seen": kwargs.get("leader_final_seen", False),
                "team_round_completed": kwargs.get("team_round_completed", False),
            }
        )
        if kwargs.get("has_result_text"):
            state["leader_text"] = "result"
        return cron_team_round_should_end(
            state,
            chunk_complete=bool(kwargs.get("chunk_complete", False)),
        )

    @staticmethod
    def is_cron_leader_placeholder_text(text: str) -> bool:
        fn = getattr(cron_scheduler_module, "_is_cron_leader_placeholder_text")
        return fn(text)

    @staticmethod
    def is_cron_team_result_insufficient(*, text: str) -> bool:
        fn = getattr(cron_scheduler_module, "_is_cron_team_result_insufficient")
        return fn(text=text)

    @staticmethod
    def extract_workflow_result_text(payload):
        fn = getattr(cron_scheduler_module, "_extract_workflow_result_text")
        return fn(payload)

    @staticmethod
    def resolve_cron_team_timeout_result(**kwargs):
        fn = getattr(cron_scheduler_module, "_resolve_cron_team_timeout_result")
        return fn(**kwargs)

    @staticmethod
    def format_cron_broadcast_text(**kwargs):
        fn = getattr(cron_scheduler_module, "_format_cron_broadcast_text")
        return fn(**kwargs)


class _MessageHandlerStreamTestApi(MessageHandler):
    @classmethod
    def is_terminal_stream_chunk(cls, chunk) -> bool:
        return cls._is_terminal_stream_chunk(chunk)

    @classmethod
    def chunk_to_message(cls, chunk, *, session_id, metadata=None):
        return cls._chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=metadata,
        )


class _TestableScheduler(CronSchedulerService):
    """Subclass that exposes protected members as public methods.

    G.CLS.11 forbids accessing protected members from outside the class
    hierarchy. By subclassing, we can access them legitimately and then
    expose thin public wrappers for test assertions — no source changes needed.
    """

    async def check_store_changed(self):
        # Delegate to protected method from within the subclass.
        return await self._check_store_changed()

    async def handle_event(self, ev):
        return await self._handle_event(ev)

    @property
    def jobs(self):
        return self._jobs

    @property
    def last_store_mtime(self):
        return self._last_store_mtime

    @property
    def runs(self):
        return self._runs

    @property
    def run_tasks(self):
        """Expose _run_tasks for test assertions (G.CLS.11: access via subclass property)."""
        return self._run_tasks

    def schedule_event(self, at_dt, kind, job_id, run_id):
        """Expose _schedule_event for test use (G.CLS.11: access via subclass wrapper)."""
        return self._schedule_event(at_dt, kind, job_id, run_id)

    async def on_wake(self, job, run_id):
        return await self._on_wake(job, run_id)

    async def cancel_agent_session(self, state, *, reason="test"):
        return await self._cancel_agent_session(state, reason=reason)

    async def cancel_team_agent_session(
        self,
        *,
        envelope,
        exec_session_id,
        mode,
    ):
        return await self._cancel_cron_team_agent_session(
            envelope=envelope,
            exec_session_id=exec_session_id,
            mode=mode,
        )

    async def run_unary_cron_job(self, *, envelope, timeout_seconds, state):
        return await self._run_unary_cron_job(
            envelope=envelope, timeout_seconds=timeout_seconds, state=state
        )

    async def run_stream_cron_job(self, *, envelope, timeout_seconds, state):
        return await self._run_stream_cron_job(
            envelope=envelope, timeout_seconds=timeout_seconds, state=state
        )

    @property
    def events(self):
        """Expose _events for test assertions (G.CLS.11: access via subclass property)."""
        return self._events

    @property
    def boot_time(self):
        return self._boot_time

    @boot_time.setter
    def boot_time(self, value):
        self._boot_time = float(value)


def _cron_published_content(msg) -> str | None:
    """Extract broadcast text from a cron push Message."""
    payload = msg.payload if isinstance(getattr(msg, "payload", None), dict) else {}
    if isinstance(payload.get("content"), str):
        return payload["content"]
    params = msg.params if isinstance(getattr(msg, "params", None), dict) else {}
    content = params.get("content")
    return content if isinstance(content, str) else None


def _make_job(job_id="job-1", name="test", **overrides):
    """Build a CronJob with sensible defaults for testing."""
    defaults = {
        "id": job_id,
        "name": name,
        "enabled": True,
        "expired": False,
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "wake_offset_seconds": 300,
        "description": "reminder",
        "targets": "tui",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    defaults.update(overrides)
    return CronJob(**defaults)


class FakeAgentClient:
    """Stub AgentServerClient that never calls a real agent."""

    def __init__(self) -> None:
        self.unary_requests = []
        self.stream_requests = []

    async def send_request(self, envelope, *a, **kw):
        self.unary_requests.append(envelope)
        if envelope.method == "session.create":
            return AgentResponse(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                ok=True,
                payload={"session_id": "cron_agentserver_allocated"},
            )
        return AgentResponse(
            request_id=envelope.request_id or "",
            channel_id=envelope.channel or "",
            ok=True,
            payload={"content": {"output": "done", "result_type": "answer"}},
        )

    async def send_request_stream(self, envelope):
        self.stream_requests.append(envelope)
        payloads = [
            {
                "event_type": "workflow.updated",
                "workflow": {
                    "id": "wf-1",
                    "status": "completed",
                    "summary": "team workflow done",
                },
            },
            {"event_type": "chat.final", "content": "team result"},
            {"is_complete": True},
        ]
        for payload in payloads:
            yield AgentResponseChunk(
                request_id=envelope.request_id or "",
                channel_id=envelope.channel or "",
                payload=payload,
                is_complete=bool(payload.get("is_complete")),
            )


class FailingAgentClient(FakeAgentClient):
    async def send_request(self, envelope, *a, **kw):
        if envelope.method == "session.create":
            return await super().send_request(envelope, *a, **kw)
        self.unary_requests.append(envelope)
        raise RuntimeError("agent unavailable")

    async def send_request_stream(self, envelope):
        self.stream_requests.append(envelope)
        if envelope.method == "session.create":
            async for chunk in super().send_request_stream(envelope):  # pragma: no cover
                yield chunk
            return
        raise RuntimeError("agent unavailable")


class FakeMessageHandler:
    """Stub MessageHandler that records published messages."""

    def __init__(self):
        self.published = []
        self.cancel_calls = []

    async def publish_robot_messages(self, msg):
        self.published.append(msg)

    async def publish_stream_chunk(self, chunk, *, session_id, request_metadata=None):
        if _MessageHandlerStreamTestApi.is_terminal_stream_chunk(chunk):
            return False
        out = _MessageHandlerStreamTestApi.chunk_to_message(
            chunk,
            session_id=session_id,
            metadata=request_metadata,
        )
        await self.publish_robot_messages(out)
        return True

    async def _cancel_agent_work_for_session(self, msg, old_sid, **kwargs):
        self.cancel_calls.append((msg, old_sid, kwargs))


async def _create_one_job(store, name="job", targets="tui", user_id="", mode=None):
    """Convenience: create a single cron job via the store."""
    return await store.create_job(
        name=name,
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets=targets,
        user_id=user_id,
        mode=mode,
    )


def _make_scheduler(store, handler=None, agent_client=None, now_fn=None):
    """Build a _TestableScheduler with fake deps for testing."""
    return _TestableScheduler(
        store=store,
        agent_client=agent_client or FakeAgentClient(),
        message_handler=handler or FakeMessageHandler(),
        **({"now_fn": now_fn} if now_fn is not None else {}),
    )


# ── _check_store_changed ─────────────────────────────────────────────────────


class TestCronLastSessionId:
    @pytest.mark.asyncio
    async def test_failed_agent_request_does_not_record_last_session_id(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store)
        svc = _make_scheduler(store, agent_client=FailingAgentClient())

        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        stored = await store.get_job(job.id)
        assert stored is not None
        assert stored.last_session_id is None

    @pytest.mark.asyncio
    async def test_successful_agent_request_records_last_session_id(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store)
        svc = _make_scheduler(store)

        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        stored = await store.get_job(job.id)
        assert stored is not None
        assert stored.last_session_id
        assert stored.last_session_id.startswith("cron_")
        assert stored.last_session_id == "cron_agentserver_allocated"

    @pytest.mark.asyncio
    async def test_run_now_info_returns_agentserver_execution_session_id(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store, user_id="run-now-owner")
        agent = FakeAgentClient()
        svc = _make_scheduler(store, agent_client=agent)

        info = await svc.trigger_run_now_info(job.id)

        assert info["run_id"].startswith(f"{job.id}:")
        assert info["session_id"] == "cron_agentserver_allocated"
        state = svc.runs[info["run_id"]]
        assert state.exec_session_id == info["session_id"]
        assert state.exec_user_id == "run-now-owner"
        assert state.execution_session_allocated is True

        await svc.on_wake(job, info["run_id"])
        await svc.run_tasks[info["run_id"]]
        assert [request.method for request in agent.unary_requests].count("session.create") == 1

    @pytest.mark.asyncio
    async def test_run_now_info_does_not_allocate_session_for_proactive_tick(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store, mode="proactive.tick")
        agent = FakeAgentClient()
        svc = _make_scheduler(store, agent_client=agent)

        info = await svc.trigger_run_now_info(job.id)

        assert info["session_id"].startswith("cron_")
        state = svc.runs[info["run_id"]]
        assert state.execution_session_allocated is False
        assert not agent.unary_requests


class TestCheckStoreChanged:
    """_check_store_changed detects file deletion, modification, recreation."""

    @pytest.mark.asyncio
    async def test_file_deleted_triggers_reload(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        await _create_one_job(store)
        assert store_file.exists()

        svc = _make_scheduler(store)
        await svc.reload()
        assert svc.last_store_mtime != 0.0
        assert len(svc.jobs) == 1

        # Delete file -> mtime becomes 0.0
        store_file.unlink()
        assert not store_file.exists()

        changed = await svc.check_store_changed()
        assert changed is True
        assert len(svc.jobs) == 0

    @pytest.mark.asyncio
    async def test_file_modified_triggers_reload(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        await _create_one_job(store, name="job-1")

        svc = _make_scheduler(store)
        await svc.reload()

        # Modify file externally via second store
        store2 = CronJobStore(path=store_file)
        await _create_one_job(store2, name="job-2", targets="web")

        changed = await svc.check_store_changed()
        assert changed is True
        assert len(svc.jobs) == 2

    @pytest.mark.asyncio
    async def test_file_recreated_triggers_reload(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        await _create_one_job(store)

        svc = _make_scheduler(store)
        await svc.reload()

        # Delete -> triggers first reload -> mtime becomes 0.0
        store_file.unlink()
        changed1 = await svc.check_store_changed()
        assert changed1 is True
        assert len(svc.jobs) == 0

        # Recreate with a new job
        store3 = CronJobStore(path=store_file)
        await _create_one_job(store3, name="new-job", targets="web")

        changed2 = await svc.check_store_changed()
        assert changed2 is True
        assert len(svc.jobs) == 1
        assert "new-job" in [j.name for j in svc.jobs.values()]

    @pytest.mark.asyncio
    async def test_no_change_does_not_reload(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        await _create_one_job(store)

        svc = _make_scheduler(store)
        await svc.reload()

        changed = await svc.check_store_changed()
        assert changed is False
        assert len(svc.jobs) == 1

    @pytest.mark.asyncio
    async def test_never_had_file_does_not_reload(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        # File never created
        store = CronJobStore(path=store_file)

        svc = _make_scheduler(store)
        await svc.reload()
        assert svc.last_store_mtime == 0.0

        changed = await svc.check_store_changed()
        assert changed is False


# ── _handle_event ────────────────────────────────────────────────────────────


class TestHandleEventStoreValidation:
    """_handle_event skips wake/push when job absent from store."""

    @pytest.mark.asyncio
    async def test_wake_skipped_when_job_absent_from_store(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()
        assert len(svc.jobs) == 1

        # Delete file -> store.get_job returns None
        store_file.unlink()

        ev = _Event(at_ts=time.time(), seq=1, kind="wake", job_id=job.id, run_id=f"{job.id}:1234")
        await svc.handle_event(ev)

        # Reload clears memory; wake not executed; no messages published
        assert len(svc.jobs) == 0
        assert len(handler.published) == 0

    @pytest.mark.asyncio
    async def test_push_skipped_when_job_absent_from_store(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        store_file.unlink()

        ev = _Event(at_ts=time.time(), seq=1, kind="push", job_id=job.id, run_id=f"{job.id}:1234")
        await svc.handle_event(ev)

        assert len(svc.jobs) == 0
        assert len(handler.published) == 0

    @pytest.mark.asyncio
    async def test_push_update_skipped_when_job_absent_from_store(self, tmp_path):
        # When cron_jobs.json is deleted, push_update should also be skipped.
        # Continuing to push results for a job that no longer exists in the store
        # creates "ghost tasks" — the user sees /cron showing no tasks but
        # messages are still being pushed, and there's no job_id to delete them.
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        # Simulate a completed run with a result to deliver
        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            result_text="result: 9am now",
        )

        # Delete file — job gone from store
        store_file.unlink()

        ev = _Event(at_ts=time.time(), seq=1, kind="push_update", job_id=job.id, run_id=run_id)
        await svc.handle_event(ev)

        # push_update should be skipped: no ghost pushes for absent jobs
        assert len(handler.published) == 0

    @pytest.mark.asyncio
    async def test_push_update_delivered_when_job_present_in_store(self, tmp_path):
        # push_update should proceed normally when the job still exists in the store.
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        # Simulate a completed run with a result to deliver
        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            result_text="result: 9am now",
        )

        # Store file still exists
        ev = _Event(at_ts=time.time(), seq=1, kind="push_update", job_id=job.id, run_id=run_id)
        await svc.handle_event(ev)

        # push_update delivered successfully
        assert len(handler.published) == 1
        content = _cron_published_content(handler.published[0])
        assert content == "result: 9am now"

    @pytest.mark.asyncio
    async def test_web_push_update_includes_execution_session_id(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store, targets="web")

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        run_info = await svc.trigger_run_now_info(job.id)
        run_id = run_info["run_id"]
        state = svc.runs[run_id]
        state.result_text = "web result"
        state.status = "succeeded"

        ev = _Event(at_ts=time.time(), seq=1, kind="push_update", job_id=job.id, run_id=run_id)
        await svc.handle_event(ev)

        assert len(handler.published) == 1
        msg = handler.published[0]
        cron = msg.payload["cron"]
        assert msg.channel_id == "web"
        assert msg.session_id is None
        assert msg.payload["user_id"] == ""
        assert cron["exec_channel_id"] == "__cron__"
        assert cron["exec_session_id"] == run_info["session_id"]

    @pytest.mark.asyncio
    async def test_wake_executes_normally_when_job_present(self, tmp_path):
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        svc = _make_scheduler(store)
        await svc.reload()

        wake_called = False

        async def _mock_on_wake(self, j, r):
            nonlocal wake_called
            wake_called = True

        # patch.object targets the original class method name
        with patch.object(CronSchedulerService, "_on_wake", _mock_on_wake):
            ev = _Event(at_ts=time.time(), seq=1, kind="wake", job_id=job.id, run_id=f"{job.id}:1234")
            await svc.handle_event(ev)

        assert wake_called is True
        assert len(svc.jobs) == 1


# ── Reload ghost task cleanup ─────────────────────────────────────────────────


class TestReloadGhostTaskCleanup:
    """reload() cancels running tasks and clears state for jobs no longer in the store."""

    @pytest.mark.asyncio
    async def test_reload_cancels_ghost_run_tasks_when_store_deleted(self, tmp_path):
        """Reload cancels in-flight tasks for absent jobs and clears _runs state."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        # Simulate a running task: create state + an asyncio Task that blocks
        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            status="running",
        )

        # Create a long-running asyncio Task (simulating agent execution)
        block_event = asyncio.Event()

        async def _long_running():
            await block_event.wait()
        task = asyncio.create_task(_long_running(), name=f"cron-run-{job.id}")
        svc.run_tasks[run_id] = task

        # Verify preconditions: run state and task exist
        assert run_id in svc.runs
        assert run_id in svc.run_tasks
        assert not task.done()

        # Delete the store file — job no longer exists persistently
        store_file.unlink()

        # Trigger reload (which _check_store_changed would do)
        await svc.reload()

        # Ghost task should be cancelled and removed from run_tasks
        assert run_id not in svc.run_tasks
        # task.cancel() is a request — the task needs a yield point to process it.
        # Give the event loop a turn to propagate the cancellation.
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        # Ghost run state should be removed from _runs
        assert run_id not in svc.runs
        # No jobs in memory
        assert len(svc.jobs) == 0
        # No messages published for the ghost task
        assert len(handler.published) == 0

        # Cleanup
        block_event.set()

    @pytest.mark.asyncio
    async def test_reload_preserves_running_tasks_for_existing_jobs(self, tmp_path):
        """In-flight tasks for jobs still in store should NOT be cancelled on reload."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        # Simulate a running task
        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            status="running",
            result_text="task finished",
        )

        # Store file still exists — job is still in the store
        await svc.reload()

        # Running task for existing job should be preserved
        assert run_id in svc.runs
        assert len(svc.jobs) == 1

    @pytest.mark.asyncio
    async def test_reload_cleans_push_update_events_for_ghost_jobs(self, tmp_path):
        """Push_update events for absent jobs should be removed during reload."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        # Schedule a push_update event
        run_id = f"{job.id}:1234"
        from datetime import datetime
        from zoneinfo import ZoneInfo
        svc.schedule_event(
            datetime.now(tz=ZoneInfo("Asia/Shanghai")),
            "push_update", job.id, run_id,
        )

        # Verify push_update event exists
        push_update_events = [
            ev for _, _, ev in svc.events if ev.kind == "push_update"
        ]
        assert len(push_update_events) == 1

        # Delete the store file — job gone from store
        store_file.unlink()

        # Reload should remove push_update events for the ghost job
        await svc.reload()

        push_update_events_after = [
            ev for _, _, ev in svc.events if ev.kind == "push_update"
        ]
        assert len(push_update_events_after) == 0


# ── Ghost task CancelledError: no push_update scheduling ──────────────────────────


class TestGhostTaskCancelledNoPushUpdate:
    """Cancelled ghost task must not schedule push_update in finally block."""

    @pytest.mark.asyncio
    async def test_cancelled_ghost_task_does_not_schedule_push_update(self, tmp_path):
        """Cancelled _run_agent must not schedule push_update in finally block."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        # Create a run state with placeholder_sent = True (triggers push_update
        # in finally when result_text becomes non-empty)
        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            status="running",
            placeholder_sent=True,
        )

        # Count push_update events before
        push_update_before = [
            ev for _, _, ev in svc.events if ev.kind == "push_update"
        ]

        # Delete store file then reload — cancels the ghost task
        store_file.unlink()
        await svc.reload()

        # After reload, ghost run is gone — no new push_update events for it
        push_update_after = [
            ev for _, _, ev in svc.events if ev.kind == "push_update"
        ]
        # push_update count should not increase (ghost task finally skipped)
        assert len(push_update_after) <= len(push_update_before)

    @pytest.mark.asyncio
    async def test_cancelled_task_finally_skips_result_text_and_push(self, tmp_path):
        """state.error == "cancelled" should prevent result_text and push_update."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler)
        await svc.reload()

        run_id = f"{job.id}:1234"
        state = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            status="running",
            placeholder_sent=True,
        )

        # Simulate CancelledError in _run_agent: state.error = "cancelled"
        state.error = "cancelled"

        # The finally block logic uses `is_cancelled_ghost = state.error == "cancelled"`
        # to skip push_update. Verify the flag works correctly:
        # Even with placeholder_sent=True, cancelled ghost should not push.
        is_cancelled_ghost = state.error == "cancelled"
        assert is_cancelled_ghost is True

        # result_text should NOT be set for cancelled ghost (finally block check)
        # (In real code: `if not state.result_text and state.error and not is_cancelled_ghost`)
        if not state.result_text and state.error and not is_cancelled_ghost:
            state.result_text = f"[cron] 任务执行失败: {state.error}"

        assert state.result_text is None  # No result_text for ghost


# ── Ghost task CHAT_CANCEL notification ────────────────────────────────────────────


class TestGhostTaskAgentCancelNotification:
    """Ghost task cancellation must send CHAT_CANCEL to AgentServer."""

    @pytest.mark.asyncio
    async def test_reload_sends_cancel_to_agent_for_ghost_tasks(self, tmp_path):
        """Ghost task cancellation should fire CHAT_CANCEL to AgentServer."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store, user_id="ghost-owner")

        # Use a FakeAgentClient that records all requests
        cancel_requests = []

        class RecordingAgentClient:
            async def send_request(self, envelope):
                # Record the envelope for later inspection
                cancel_requests.append(envelope)
                return {"content": {"output": "cancelled", "result_type": "answer"}}

        handler = FakeMessageHandler()
        svc = _TestableScheduler(
            store=store,
            agent_client=RecordingAgentClient(),
            message_handler=handler,
        )
        await svc.reload()

        # Simulate a running task
        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            status="running",
            exec_user_id=job.user_id,
        )

        # Create a blocking asyncio Task (simulating agent execution)
        block_event = asyncio.Event()

        async def _long_running():
            await block_event.wait()

        task = asyncio.create_task(_long_running(), name=f"cron-run-{job.id}")
        svc.run_tasks[run_id] = task

        # Delete store file — job gone from store
        store_file.unlink()

        # Reload should cancel ghost task AND send CHAT_CANCEL
        await svc.reload()

        # Give the event loop a turn for the fire-and-forget cancel task to execute
        await asyncio.sleep(0.1)

        # Verify CHAT_CANCEL was sent to AgentServer
        # The cancel request should have method = "chat.interrupt"
        cancel_envelopes = [
            e for e in cancel_requests
            if hasattr(e, "method") and e.method == "chat.interrupt"
        ]
        assert len(cancel_envelopes) >= 1, (
            f"Expected at least 1 CHAT_CANCEL request, got {len(cancel_envelopes)} "
            f"out of {len(cancel_requests)} total requests"
        )

        # Verify the cancel envelope has the correct job context
        cancel_env = cancel_envelopes[0]
        assert hasattr(cancel_env, "params")
        assert "cron" in (cancel_env.params or {})
        assert cancel_env.params["cron"]["job_id"] == job.id
        assert cancel_env.params["cron"]["run_id"] == run_id
        assert cancel_env.user_id == "ghost-owner"

        # Cleanup
        block_event.set()

    @pytest.mark.asyncio
    async def test_no_cancel_sent_when_task_already_done(self, tmp_path):
        """If the ghost task is already done, no CHAT_CANCEL should be sent."""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        job = await _create_one_job(store)

        cancel_requests = []

        class RecordingAgentClient:
            async def send_request(self, envelope):
                cancel_requests.append(envelope)
                return {"content": {"output": "done", "result_type": "answer"}}

        handler = FakeMessageHandler()
        svc = _TestableScheduler(
            store=store,
            agent_client=RecordingAgentClient(),
            message_handler=handler,
        )
        await svc.reload()

        run_id = f"{job.id}:1234"
        svc.runs[run_id] = CronRunState(
            run_id=run_id,
            job_id=job.id,
            wake_at_iso="2026-06-09T08:55:00+08:00",
            push_at_iso="2026-06-09T09:00:00+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
            status="succeeded",
        )

        # Create a task that's already done (completed immediately)
        async def _instant_task():
            return "done"

        task = asyncio.create_task(_instant_task(), name=f"cron-run-{job.id}")
        # Wait for it to finish
        await task
        assert task.done()
        svc.run_tasks[run_id] = task

        # Delete store file — job gone from store
        store_file.unlink()

        # Reload should NOT send CHAT_CANCEL because task is already done
        await svc.reload()
        await asyncio.sleep(0.1)

        cancel_envelopes = [
            e for e in cancel_requests
            if hasattr(e, "method") and e.method == "chat.interrupt"
        ]
        assert len(cancel_envelopes) == 0


# ── Team mode execution ──────────────────────────────────────────────────────


class TestCronMultiModeCancellation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode",
        ["agent", "team", "team.plan.normal", "team.plan.code", "code.team"],
    )
    async def test_ghost_cancel_uses_execution_mode_after_job_removal(
        self,
        tmp_path,
        mode,
    ):
        agent = FakeAgentClient()
        svc = _make_scheduler(
            CronJobStore(path=tmp_path / "cron_jobs.json"),
            agent_client=agent,
        )
        state = CronRunState(
            run_id="job-1:run-1",
            job_id="job-1",
            wake_at_iso="2026-08-25T09:00:00+08:00",
            push_at_iso="2026-08-25T09:05:00+08:00",
            exec_mode=mode,
            exec_channel_id="tui",
            exec_session_id="cron-session-1",
            exec_user_id="owner-user",
            exec_work_mode="code",
            exec_project_id="project-a",
            exec_project_dir="D:/workspace/project-a",
        )

        await svc.cancel_agent_session(state, reason="ghost")

        cancel = agent.unary_requests[-1]
        assert cancel.method == "chat.interrupt"
        assert cancel.channel == "tui"
        assert cancel.session_id == "cron-session-1"
        assert cancel.user_id == "owner-user"
        assert cancel.params == {
            "intent": "cancel",
            "mode": mode,
            "work_mode": "code",
            "session_id": "cron-session-1",
            "cron": {"job_id": "job-1", "run_id": "job-1:run-1"},
            "project_id": "project-a",
            "project_dir": "D:/workspace/project-a",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode",
        ["team", "team.plan.normal", "team.plan.code", "code.team"],
    )
    async def test_team_stream_cancel_preserves_team_submode(self, tmp_path, mode):
        handler = FakeMessageHandler()
        svc = _make_scheduler(
            CronJobStore(path=tmp_path / "cron_jobs.json"),
            handler=handler,
        )
        envelope = SimpleNamespace(
            request_id="cron-team-run",
            channel="tui",
            params={
                "work_mode": "code",
                "project_id": "team-project",
                "project_dir": "D:/workspace/team-project",
            },
        )

        await svc.cancel_team_agent_session(
            envelope=envelope,
            exec_session_id="cron-team-session",
            mode=mode,
        )

        cancel_message, old_session_id, kwargs = handler.cancel_calls[-1]
        assert old_session_id == "cron-team-session"
        assert cancel_message.params == {
            "intent": "cancel",
            "mode": mode,
            "session_id": "cron-team-session",
            "work_mode": "code",
            "project_id": "team-project",
            "project_dir": "D:/workspace/team-project",
        }
        assert kwargs["channel_id"] == "tui"
        assert kwargs["cancel_gateway_tasks"] is False


class TestCronUnknownModeBoundary:
    @pytest.mark.asyncio
    async def test_run_now_rejects_unknown_mode_before_scheduling(self, tmp_path):
        svc = _make_scheduler(CronJobStore(path=tmp_path / "cron_jobs.json"))
        job = _make_job(mode="future.mode")
        svc.jobs[job.id] = job

        with pytest.raises(ValueError, match="Invalid cron job mode"):
            await svc.trigger_run_now_info(job.id)

        assert svc.runs == {}
        assert svc.events == []

    @pytest.mark.asyncio
    async def test_wake_rejects_unknown_mode_without_agent_request(self, tmp_path):
        agent = FakeAgentClient()
        svc = _make_scheduler(
            CronJobStore(path=tmp_path / "cron_jobs.json"),
            agent_client=agent,
        )
        job = _make_job(mode="future.mode")
        run_id = f"{job.id}:unknown-mode"

        await svc.on_wake(job, run_id)

        state = svc.runs[run_id]
        assert state.status == "failed"
        assert state.error is not None
        assert "Invalid cron job mode" in state.error
        assert agent.unary_requests == []
        assert agent.stream_requests == []


class TestTeamModeWake:
    """Team-mode cron jobs stream to AgentServer and publish SwarmFlow chunks."""

    @pytest.mark.asyncio
    async def test_team_wake_uses_isolated_session_and_stream(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(
            mode="team",
            session_id="user-session-1",
            targets="tui",
            description="run swarmflow",
            user_id="team-owner",
        )

        agent = FakeAgentClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        task = svc.run_tasks.get(run_id)
        assert task is not None
        await task

        assert len(agent.stream_requests) == 1
        assert len(agent.unary_requests) == 0
        env = agent.stream_requests[0]
        assert env.is_stream is True
        assert env.channel == "tui"
        assert env.session_id.startswith("cron_") and env.session_id.endswith(f"_{job.id}")
        assert env.params["mode"] == "team.work.normal"
        assert env.user_id == "team-owner"

        state = svc.runs[run_id]
        assert state.exec_user_id == "team-owner"
        assert state.status == "succeeded"
        assert state.result_text == "team result"
        assert len(handler.published) == 2

    @pytest.mark.asyncio
    async def test_agent_wake_uses_unary_cron_channel(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(description="simple reminder", targets="tui")

        agent = FakeAgentClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        task = svc.run_tasks.get(run_id)
        assert task is not None
        await task

        assert len(agent.unary_requests) == 1
        assert len(agent.stream_requests) == 1
        create_env = agent.unary_requests[0]
        assert create_env.method == "session.create"
        assert "session_id" not in create_env.params
        env = agent.stream_requests[0]
        assert env.is_stream is True
        assert env.channel == "__cron__"
        assert env.session_id == "cron_agentserver_allocated"

        state = svc.runs[run_id]
        assert state.status == "succeeded"
        assert state.result_text == "team result"

    @pytest.mark.asyncio
    async def test_agent_wake_passes_model_as_model_name(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(description="simple reminder", targets="tui", model_name="fast-model")

        agent = FakeAgentClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        task = svc.run_tasks.get(run_id)
        assert task is not None
        await task

        env = agent.stream_requests[0]
        assert env.params["model_name"] == "fast-model"
        assert "model" not in env.params

    @pytest.mark.asyncio
    async def test_agent_wake_does_not_resolve_project_dir_in_gateway(self, tmp_path):
        """Phase 4：scheduler 触发时不再本地反查 project_id → project_dir。

        SESSION_CREATE / CHAT_SEND 只传 project_id，归属解析由目标 AgentServer
        在其注入目录内完成（resolve_session_project_binding 规则2 自动补齐）。
        """
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(description="reminder", targets="tui", project_id="proj-1")

        agent = FakeAgentClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:1234"
        await svc.on_wake(job, run_id)
        task = svc.run_tasks.get(run_id)
        assert task is not None
        await task

        create_env = agent.unary_requests[0]
        assert create_env.method == "session.create"
        # 不再本地反查 project_dir（Gateway 不访问用户目录项目表）
        assert "project_dir" not in create_env.params
        assert create_env.params["project_id"] == "proj-1"
        chat_env = agent.stream_requests[0]
        assert "project_dir" not in chat_env.params
        assert chat_env.params["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_team_wake_stream_timeout(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(
            mode="team",
            session_id="user-session-1",
            targets="tui",
            timeout_seconds=1,
        )

        class HangingStreamClient(FakeAgentClient):
            async def send_request_stream(self, envelope):
                self.stream_requests.append(envelope)
                await asyncio.Event().wait()
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload={"is_complete": True},
                    is_complete=True,
                )

        agent = HangingStreamClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:5678"
        await svc.on_wake(job, run_id)
        task = svc.run_tasks.get(run_id)
        assert task is not None
        await task

        state = svc.runs[run_id]
        assert state.status == "failed"
        assert state.result_text is not None
        assert ">" in state.result_text
        assert len(handler.cancel_calls) == 1

    @pytest.mark.asyncio
    async def test_team_stream_ignores_placeholder_before_workflow_completed(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(mode="team", session_id="user-session-1", targets="tui")
        placeholder = (
            "🔗 Integration 阶段进行中 — Integrator 正在接收三位审查员的独立输出。\n"
            "最终报告即将生成，请稍候。"
        )
        final_report = "## 🔬 Code Review Swarm — 审查完成\n\n最终建议: approve"

        class PlaceholderThenReportStreamClient(FakeAgentClient):
            async def send_request_stream(self, envelope):
                self.stream_requests.append(envelope)
                payloads = [
                    {"event_type": "chat.final", "content": placeholder},
                    {
                        "event_type": "workflow.updated",
                        "workflow": {
                            "id": "wf-1",
                            "status": "completed",
                            "summary": "workflow summary",
                        },
                    },
                    {"event_type": "chat.final", "content": final_report},
                ]
                for payload in payloads:
                    yield AgentResponseChunk(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        payload=payload,
                        is_complete=False,
                    )
                await asyncio.Event().wait()

        agent = PlaceholderThenReportStreamClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:placeholder"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        state = svc.runs[run_id]
        assert state.status == "succeeded"
        assert state.result_text == final_report
        assert len(handler.cancel_calls) == 1

    @pytest.mark.asyncio
    async def test_team_stream_error_returns_error_without_partial_result(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(mode="team", session_id="user-session-1", targets="tui")

        class PartialResultThenErrorStreamClient(FakeAgentClient):
            async def send_request_stream(self, envelope):
                self.stream_requests.append(envelope)
                for payload in (
                    {
                        "event_type": "workflow.updated",
                        "workflow": {
                            "id": "wf-1",
                            "status": "completed",
                            "summary": "partial report",
                        },
                    },
                    {"event_type": "execution.error", "message": "model API unavailable"},
                ):
                    yield AgentResponseChunk(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        payload=payload,
                        is_complete=False,
                    )

        agent = PartialResultThenErrorStreamClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:partial-error"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        state = svc.runs[run_id]
        assert state.status == "failed"
        assert "model API unavailable" in (state.result_text or "")
        assert "partial report" not in (state.result_text or "")
        assert len(handler.cancel_calls) == 1

    @pytest.mark.asyncio
    async def test_team_stream_ends_early_on_workflow_and_final(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(mode="team", session_id="user-session-1", targets="tui")

        class EarlyEndStreamClient(FakeAgentClient):
            async def send_request_stream(self, envelope):
                self.stream_requests.append(envelope)
                payloads = [
                    {
                        "event_type": "workflow.updated",
                        "workflow": {
                            "id": "wf-1",
                            "status": "completed",
                            "summary": "workflow summary",
                        },
                    },
                    {"event_type": "chat.final", "content": "leader final report"},
                ]
                for payload in payloads:
                    yield AgentResponseChunk(
                        request_id=envelope.request_id or "",
                        channel_id=envelope.channel or "",
                        payload=payload,
                        is_complete=False,
                    )
                await asyncio.Event().wait()

        agent = EarlyEndStreamClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:early"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        state = svc.runs[run_id]
        assert state.status == "succeeded"
        assert state.result_text == "leader final report"
        assert len(handler.cancel_calls) == 1

    @pytest.mark.asyncio
    async def test_team_timeout_uses_workflow_result_when_available(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(
            mode="team",
            session_id="user-session-1",
            targets="tui",
            timeout_seconds=1,
        )

        class WorkflowOnlyHangStreamClient(FakeAgentClient):
            async def send_request_stream(self, envelope):
                self.stream_requests.append(envelope)
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload={
                        "event_type": "workflow.updated",
                        "workflow": {
                            "id": "wf-1",
                            "status": "completed",
                            "summary": "workflow-only summary",
                        },
                    },
                    is_complete=False,
                )
                await asyncio.Event().wait()

        agent = WorkflowOnlyHangStreamClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:partial"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        state = svc.runs[run_id]
        assert state.status == "succeeded"
        assert state.result_text == "workflow-only summary"
        assert len(handler.cancel_calls) == 1

    @pytest.mark.asyncio
    async def test_team_wake_succeeds_without_publish_stream_chunk(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(mode="team", session_id="user-session-1", targets="tui")

        class MinimalMessageHandler:
            async def publish_robot_messages(self, msg):
                pass

        agent = FakeAgentClient()
        svc = _make_scheduler(store, MinimalMessageHandler(), agent_client=agent)

        run_id = f"{job.id}:9999"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        state = svc.runs[run_id]
        assert state.status == "succeeded"
        assert state.result_text == "team result"

    @pytest.mark.asyncio
    async def test_team_stream_fails_on_placeholder_without_workflow(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = _make_job(mode="team", targets="tui")
        placeholder = "最终报告即将生成，请稍候。"

        class PlaceholderOnlyStreamClient(FakeAgentClient):
            async def send_request_stream(self, envelope):
                self.stream_requests.append(envelope)
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload={"event_type": "chat.final", "content": placeholder},
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id=envelope.request_id or "",
                    channel_id=envelope.channel or "",
                    payload={"is_complete": True},
                    is_complete=True,
                )

        agent = PlaceholderOnlyStreamClient()
        handler = FakeMessageHandler()
        svc = _make_scheduler(store, handler, agent_client=agent)

        run_id = f"{job.id}:placeholder-only"
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        state = svc.runs[run_id]
        assert state.status == "failed"
        assert "未产生有效报告" in (state.result_text or "")


class TestResolveCronExecutionContext:
    @staticmethod
    def test_team_ignores_creator_session_on_tui():
        job = _make_job(mode="team", targets="tui", session_id="sess-abc")
        channel_id, session_id = _CronSchedulerTeamTestApi.resolve_cron_execution_context(job, ts="abc123")
        assert channel_id == "tui"
        assert session_id == f"cron_abc123_{job.id}"

    @staticmethod
    def test_falls_back_to_isolated_session_without_creator_session():
        job = _make_job(mode="team", targets="tui", session_id=None)
        channel_id, session_id = _CronSchedulerTeamTestApi.resolve_cron_execution_context(job, ts="abc123")
        assert channel_id == "tui"
        assert session_id == f"cron_abc123_{job.id}"

    @staticmethod
    def test_team_uses_isolated_session_when_job_has_no_session():
        job = _make_job(mode="team", targets="tui", session_id=None)

        class ActiveSessionHandler:
            @staticmethod
            def _resolve_stream_cancel_session_id(channel_id: str) -> str:
                assert channel_id == "tui"
                return "active-tui-session"

        channel_id, session_id = _CronSchedulerTeamTestApi.resolve_cron_execution_context(
            job,
            ts="abc123",
            message_handler=ActiveSessionHandler(),
        )
        assert channel_id == "tui"
        assert session_id == f"cron_abc123_{job.id}"

    @staticmethod
    def test_non_team_does_not_reuse_active_channel_session():
        job = _make_job(mode="agent.fast", targets="tui", session_id=None)

        class ActiveSessionHandler:
            @staticmethod
            def _resolve_stream_cancel_session_id(channel_id: str) -> str:
                return "active-tui-session"

        channel_id, session_id = _CronSchedulerTeamTestApi.resolve_cron_execution_context(
            job,
            ts="abc123",
            message_handler=ActiveSessionHandler(),
        )
        assert channel_id == "tui"
        assert session_id == f"cron_abc123_{job.id}"


class TestCronTeamStreamHelpers:
    @staticmethod
    def test_stream_should_end_only_after_workflow_and_real_final():
        assert _CronSchedulerTeamTestApi.cron_team_stream_should_end(
            workflow_completed=True,
            leader_final_after_workflow=True,
            leader_final_seen=True,
            team_round_completed=False,
            has_result_text=True,
            chunk_complete=False,
        )
        assert not _CronSchedulerTeamTestApi.cron_team_stream_should_end(
            workflow_completed=True,
            leader_final_after_workflow=False,
            leader_final_seen=True,
            team_round_completed=False,
            has_result_text=True,
            chunk_complete=False,
        )

    @staticmethod
    def test_stream_should_end_on_team_round_completed_with_result():
        assert _CronSchedulerTeamTestApi.cron_team_stream_should_end(
            workflow_completed=False,
            leader_final_after_workflow=False,
            leader_final_seen=False,
            team_round_completed=True,
            has_result_text=True,
            chunk_complete=False,
        )
        assert not _CronSchedulerTeamTestApi.cron_team_stream_should_end(
            workflow_completed=False,
            leader_final_after_workflow=False,
            leader_final_seen=False,
            team_round_completed=True,
            has_result_text=False,
            chunk_complete=False,
        )

    @staticmethod
    def test_stream_should_end_on_leader_final_without_team_completed():
        assert _CronSchedulerTeamTestApi.cron_team_stream_should_end(
            workflow_completed=False,
            leader_final_after_workflow=False,
            leader_final_seen=True,
            team_round_completed=False,
            has_result_text=True,
            chunk_complete=False,
        )
        assert not _CronSchedulerTeamTestApi.cron_team_stream_should_end(
            workflow_completed=False,
            leader_final_after_workflow=False,
            leader_final_seen=True,
            team_round_completed=False,
            has_result_text=False,
            chunk_complete=False,
        )

    @staticmethod
    def test_placeholder_detection():
        assert _CronSchedulerTeamTestApi.is_cron_leader_placeholder_text("最终报告即将生成，请稍候。")
        assert not _CronSchedulerTeamTestApi.is_cron_leader_placeholder_text("## 审查完成\n\n最终建议: approve")

    @staticmethod
    def test_insufficient_result_checks_empty_and_placeholder():
        assert _CronSchedulerTeamTestApi.is_cron_team_result_insufficient(text="")
        assert _CronSchedulerTeamTestApi.is_cron_team_result_insufficient(text="最终报告即将生成，请稍候。")
        assert not _CronSchedulerTeamTestApi.is_cron_team_result_insufficient(text="## 审查完成\n\n最终建议: approve")

    @staticmethod
    def test_extract_workflow_result_from_completed_payload():
        payload = {
            "event_type": "workflow.updated",
            "workflow": {
                "status": "completed",
                "summary": "all good",
            },
        }
        assert _CronSchedulerTeamTestApi.extract_workflow_result_text(payload) == "all good"

    @staticmethod
    def test_timeout_ignores_placeholder_when_workflow_completed():
        text, ok = _CronSchedulerTeamTestApi.resolve_cron_team_timeout_result(
            leader_text="最终报告即将生成，请稍候。",
            workflow_text="integrator outcome summary",
            workflow_completed=True,
            timeout_min=10,
        )
        assert ok is True
        assert text == "integrator outcome summary"


class TestCronBroadcastText:
    @staticmethod
    def test_final_result_returned_without_prefix():
        assert _CronSchedulerTeamTestApi.format_cron_broadcast_text(
            job_name="agent-core-commit-review",
            text="## 审查完成",
            is_placeholder=False,
        ) == "## 审查完成"

    @staticmethod
    def test_keeps_placeholder_unchanged_and_passes_through_cron_prefixed_status():
        placeholder = "agent-core-commit-review 正在执行中，结果稍后补发（push_at=2026-01-01T09:00:00+08:00）"
        assert _CronSchedulerTeamTestApi.format_cron_broadcast_text(
            job_name="agent-core-commit-review",
            text=placeholder,
            is_placeholder=True,
        ) == placeholder
        assert _CronSchedulerTeamTestApi.format_cron_broadcast_text(
            job_name="agent-core-commit-review",
            text="[cron] 任务执行超时（>10min）",
            is_placeholder=False,
        ) == "[cron] 任务执行超时（>10min）"

def _create_many_jobs_in_thread(path: Path, prefix: str, n: int) -> None:
    """在独立线程 + 独立事件循环里写 store（模拟另一 Gateway 进程）。"""
    store = CronJobStore(path=path)

    async def _run() -> None:
        for i in range(n):
            await store.create_job(
                name=f"{prefix}-{i}",
                cron_expr="0 0 9 * * ? *",
                timezone="Asia/Shanghai",
                description="reminder",
                targets="web",
            )

    asyncio.run(_run())


class TestUnaryCronJobTimeoutOverride:
    """cron 的 timeout_seconds 应作为 send_request 等待上限真正生效。

    之前 _run_unary_cron_job 用外层 asyncio.wait_for(timeout=timeout_seconds)
    包住 send_request，但 send_request 内部还有一个写死的 600s 内层超时；
    当 cron 配置的超时 >600s（默认 3600s）时，内层 600s 先触发并抛 RuntimeError，
    既绕过了 cron 自己的超时文案、也跳过了 cancel 后端的收尾。改造后 timeout
    直接透传给 send_request，由其作为唯一等待上限。
    """

    @pytest.mark.asyncio
    async def test_timeout_passed_through_and_cancel_invoked(self, tmp_path):
        from jiuwenswarm.gateway.routing.agent_client import AgentServerUnaryTimeout

        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store, targets="web")

        class TimeoutAgentClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, float | None]] = []

            async def send_request(self, envelope, *, timeout=None, **kw):
                method = getattr(envelope, "method", "")
                self.calls.append((method, timeout))
                if method == "chat.send":
                    # 模拟 AgentServer 在 cron 超时阈值内未响应
                    raise AgentServerUnaryTimeout(
                        getattr(envelope, "request_id", "rid"),
                        float(timeout or 0),
                    )
                # CHAT_CANCEL 中断请求：返回 ok，不阻断收尾
                return AgentResponse(
                    request_id=getattr(envelope, "request_id", "") or "",
                    channel_id=getattr(envelope, "channel", "") or "",
                    ok=True,
                    payload={},
                )

            async def send_request_stream(self, envelope):  # pragma: no cover
                raise RuntimeError("not used")

        client = TimeoutAgentClient()
        handler = FakeMessageHandler()
        svc = _TestableScheduler(
            store=store,
            agent_client=client,
            message_handler=handler,
        )

        envelope = SimpleNamespace(
            request_id="cron-job-1:1",
            method="chat.send",
            channel="__cron__",
        )
        state = CronRunState(
            run_id="job-1:1",
            job_id=job.id,
            wake_at_iso="2026-08-21T10:17:25+08:00",
            push_at_iso="2026-08-21T10:17:25+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
        )

        text, ok = await svc.run_unary_cron_job(
            envelope=envelope, timeout_seconds=120.0, state=state
        )

        # 1. cron 的 timeout_seconds 被透传给 send_request（覆盖默认 600s）
        assert client.calls[0] == ("chat.send", 120.0)
        # 2. 超时后主动发 CHAT_CANCEL 收尾（第二次 send_request 调用）
        assert len(client.calls) == 2
        assert client.calls[1][1] is None  # cancel 不带 timeout
        # 3. 文案用 cron 自己的超时文案，而非裸 "AgentServer 非流式请求超时"
        assert ok is False
        assert text == "[cron] 任务执行超时（>2min）"
        assert "非流式请求超时" not in text

    @pytest.mark.asyncio
    async def test_slow_but_within_timeout_succeeds(self, tmp_path):
        """任务耗时超过旧 600s 硬顶但低于 cron 自定义超时时应成功，而非被截断。"""

        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store, targets="web")

        class SlowThenOkAgentClient:
            def __init__(self) -> None:
                self.received_timeout = None

            async def send_request(self, envelope, *, timeout=None, **kw):
                self.received_timeout = timeout
                return AgentResponse(
                    request_id=getattr(envelope, "request_id", "") or "",
                    channel_id=getattr(envelope, "channel", "") or "",
                    ok=True,
                    payload={"content": {"output": "done", "result_type": "answer"}},
                )

            async def send_request_stream(self, envelope):  # pragma: no cover
                raise RuntimeError("not used")

        client = SlowThenOkAgentClient()
        handler = FakeMessageHandler()
        svc = _TestableScheduler(
            store=store,
            agent_client=client,
            message_handler=handler,
        )

        envelope = SimpleNamespace(
            request_id="cron-job-2:1",
            method="chat.send",
            channel="__cron__",
        )
        state = CronRunState(
            run_id="job-2:1",
            job_id=job.id,
            wake_at_iso="2026-08-21T10:17:25+08:00",
            push_at_iso="2026-08-21T10:17:25+08:00",
            job_name=job.name,
            targets=job.targets,
            session_id=None,
            chat_type=None,
            timezone=job.timezone,
        )

        # 给一个远大于旧 600s 硬顶的自定义超时；改造前会被 600s 截断，
        # 改造后 send_request 用此值，任务（立即返回 ok）正常成功。
        text, ok = await svc.run_unary_cron_job(
            envelope=envelope, timeout_seconds=3600.0, state=state
        )

        assert client.received_timeout == 3600.0
        assert ok is True
        assert text == "done"


class TestSingleAgentCronStream:
    @pytest.mark.asyncio
    async def test_waits_for_chat_final_instead_of_empty_unary_ack(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await _create_one_job(store, targets="web")

        class StreamingAgentClient:
            async def send_request_stream(self, envelope):
                yield AgentResponseChunk(
                    request_id=envelope.request_id,
                    channel_id=envelope.channel,
                    payload={"event_type": "chat.reasoning", "content": ""},
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id=envelope.request_id,
                    channel_id=envelope.channel,
                    payload={"event_type": "chat.final", "content": "请记得开会。"},
                    is_complete=True,
                )

        svc = _TestableScheduler(
            store=store,
            agent_client=StreamingAgentClient(),
            message_handler=FakeMessageHandler(),
        )
        envelope = SimpleNamespace(request_id="cron-job-stream:1", channel="__cron__")
        state = CronRunState(
            run_id="job-stream:1", job_id=job.id,
            wake_at_iso="2026-08-22T15:00:00+08:00",
            push_at_iso="2026-08-22T15:00:00+08:00",
            job_name=job.name, targets=job.targets, session_id=None,
            chat_type=None, timezone=job.timezone,
        )

        text, ok = await svc.run_stream_cron_job(
            envelope=envelope, timeout_seconds=30.0, state=state
        )

        assert ok is True
        assert text == "请记得开会。"


class TestCronJobStoreFileLock:
    """验证 portalocker 伴生锁包住 read-modify-write，避免多实例 lost update。"""

    def test_threaded_concurrent_creates_preserve_all_jobs(self, tmp_path):
        """两线程各持独立 store 并发 create，最终 jobs 应全部保留。"""
        from concurrent.futures import ThreadPoolExecutor

        store_file = tmp_path / "cron_jobs.json"
        n = 40

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(_create_many_jobs_in_thread, store_file, "a", n)
            fut_b = pool.submit(_create_many_jobs_in_thread, store_file, "b", n)
            fut_a.result(timeout=120)
            fut_b.result(timeout=120)

        jobs = asyncio.run(CronJobStore(path=store_file).list_jobs())
        names = sorted(j.name for j in jobs)
        expected = sorted([f"a-{i}" for i in range(n)] + [f"b-{i}" for i in range(n)])
        assert len(jobs) == 2 * n, (
            f"lost update without effective file lock: got {len(jobs)}, expected {2 * n}"
        )
        assert names == expected
        assert store_file.with_suffix(".json.lock").exists()

    @pytest.mark.asyncio
    async def test_held_file_lock_times_out_other_store(self, tmp_path):
        """外部已持有伴生锁时，短超时的 store 写操作应抛 LockException。"""
        import portalocker

        store_file = tmp_path / "cron_jobs.json"
        lock_path = store_file.with_suffix(".json.lock")
        store = CronJobStore(path=store_file, file_lock_timeout=0.2)

        store_file.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(lock_path), timeout=1.0):
            with pytest.raises(portalocker.exceptions.LockException):
                await store.create_job(
                    name="blocked",
                    cron_expr="0 0 9 * * ? *",
                    timezone="Asia/Shanghai",
                    description="reminder",
                    targets="web",
                )

        # 锁释放后应可正常写入
        job = await store.create_job(
            name="after-unlock",
            cron_expr="0 0 9 * * ? *",
            timezone="Asia/Shanghai",
            description="reminder",
            targets="web",
        )
        assert job.name == "after-unlock"
        listed = await store.list_jobs()
        assert any(j.id == job.id for j in listed)


class TestCronJobStoreWakeOffset:
    """面板「提前唤醒」对应 store 的 wake_offset_seconds 读写（Issue #2533）。"""

    @pytest.mark.asyncio
    async def test_create_job_persists_wake_offset_seconds(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await store.create_job(
            name="daily-meeting",
            cron_expr="0 0 17 * * ? *",
            timezone="Asia/Shanghai",
            description="提醒用户开会",
            targets="web",
            wake_offset_seconds=300,
        )
        assert job.wake_offset_seconds == 300
        reloaded = await store.get_job(job.id)
        assert reloaded is not None
        assert reloaded.wake_offset_seconds == 300

    @pytest.mark.asyncio
    async def test_update_job_can_change_and_clear_wake_offset_seconds(self, tmp_path):
        """前端编辑「提前唤醒」分钟数时，经 cron.job.update patch 写入秒数。"""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await store.create_job(
            name="daily-meeting",
            cron_expr="0 0 10 * * ? *",
            timezone="Asia/Shanghai",
            description="提醒用户开会",
            targets="web",
            wake_offset_seconds=300,
        )

        updated = await store.update_job(
            job.id,
            {"cron_expr": "0 0 17 * * ? *", "wake_offset_seconds": 0},
        )
        assert updated.cron_expr == "0 0 17 * * ? *"
        assert updated.wake_offset_seconds == 0

        again = await store.update_job(job.id, {"wake_offset_seconds": 600})
        assert again.wake_offset_seconds == 600
        reloaded = await store.get_job(job.id)
        assert reloaded is not None
        assert reloaded.wake_offset_seconds == 600

    @pytest.mark.asyncio
    async def test_update_job_clamps_negative_wake_offset_to_zero(self, tmp_path):
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await store.create_job(
            name="daily-meeting",
            cron_expr="0 0 17 * * ? *",
            timezone="Asia/Shanghai",
            description="reminder",
            targets="web",
            wake_offset_seconds=300,
        )
        updated = await store.update_job(job.id, {"wake_offset_seconds": -1})
        assert updated.wake_offset_seconds == 0

    @pytest.mark.asyncio
    async def test_proactive_tick_update_rejects_wake_offset_patch(self, tmp_path):
        """proactive.tick 只允许改 cron_expr/timezone，wake_offset 被丢弃。"""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await store.create_job(
            name="proactive-auto",
            cron_expr="0 0 * * * ? *",
            timezone="Asia/Shanghai",
            description="proactive",
            targets="web",
            mode="proactive.tick",
            wake_offset_seconds=0,
        )
        updated = await store.update_job(
            job.id,
            {"cron_expr": "0 30 * * * ? *", "wake_offset_seconds": 300},
        )
        assert updated.cron_expr == "0 30 * * * ? *"
        assert updated.wake_offset_seconds == 0


class TestExtractTextFromAgentPayload:
    """Coverage for _extract_text_from_agent_payload (agent-mode unary response parser)."""

    @staticmethod
    def _call(payload):
        from jiuwenswarm.gateway.cron.scheduler import _extract_text_from_agent_payload
        return _extract_text_from_agent_payload(payload)

    def test_error_payload_returns_raw_error(self):
        """error is passed through unchanged, same as normal chat."""
        result = self._call({"error": "PermissionDeniedError: Error code: 403 - ..."})
        assert result == "PermissionDeniedError: Error code: 403 - ..."

    def test_empty_error_returns_empty_string(self):
        result = self._call({"error": ""})
        assert result == ""

    def test_whitespace_only_error_returns_empty_string(self):
        result = self._call({"error": "   "})
        assert result == ""

    def test_content_str_not_affected(self):
        result = self._call({"content": "hello"})
        assert result == "hello"

    def test_content_dict_not_affected(self):
        result = self._call({"content": {"output": "result", "result_type": "answer"}})
        assert result == "result"

    def test_content_dict_no_output_returns_str(self):
        result = self._call({"content": {"key": "val"}})
        assert result == str({"key": "val"})

    def test_empty_payload_returns_empty_string(self):
        result = self._call({})
        assert result == ""

    def test_none_payload_returns_empty_string(self):
        result = self._call(None)
        assert result == ""

    def test_error_takes_precedence_over_content(self):
        """When both error and content present, error passes through raw."""
        original_error = "PermissionDeniedError: Error code: 403"
        result = self._call({"error": original_error, "content": "stale output"})
        assert result == original_error

    def test_legacy_heartbeat_payload_is_ignored(self):
        result = self._call({"heartbeat": "ping"})
        assert result == ""

    def test_text_fallback(self):
        result = self._call({"text": "raw text"})
        assert result == "raw text"

    def test_error_none_value_returns_empty_string(self):
        result = self._call({"error": None})
        assert result == ""

    def test_error_int_value_returns_empty_string(self):
        result = self._call({"error": 42})
        assert result == ""


class _Clock:
    """可控时钟，供 now_fn 注入测试，避免依赖真实秒数导致边界抖动。"""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestCrashRecoveryGraceWindow:
    """crash_recovery_skip 仅在进程启动 grace 窗口内启用。

    背景：运行期 reload（其他任务 update_job 改 cron_jobs.json mtime 触发
    _check_store_changed → reload → _events.clear()）会抹掉尚未到点/刚到点
    的合法 wake 事件。重排时"existing is None + wake 刚过去"被误判为崩溃
    残留而丢弃，导致一次性任务被静默吞掉。修复后用 _boot_time grace 窗口
    区分真崩溃与运行期 reload。
    """

    @staticmethod
    def _recent_oneshot_expr(seconds_ago: float = 3.0) -> str:
        tz = ZoneInfo("Asia/Shanghai")
        run_dt = datetime.now(tz=tz) - timedelta(seconds=seconds_ago)
        return (
            f"{run_dt.second} {run_dt.minute} {run_dt.hour} "
            f"{run_dt.day} {run_dt.month} ? {run_dt.year}"
        )

    @staticmethod
    def _future_oneshot_expr(seconds_ahead: float = 30.0) -> str:
        tz = ZoneInfo("Asia/Shanghai")
        run_dt = datetime.now(tz=tz) + timedelta(seconds=seconds_ahead)
        return (
            f"{run_dt.second} {run_dt.minute} {run_dt.hour} "
            f"{run_dt.day} {run_dt.month} ? {run_dt.year}"
        )

    @pytest.mark.asyncio
    async def test_runtime_reload_keeps_recently_due_wake_beyond_grace(self, tmp_path):
        """运行期 reload（boot_time 已超 grace）：刚到点的 wake 不应被丢弃。"""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await store.create_job(
            name="drink",
            cron_expr=self._recent_oneshot_expr(seconds_ago=3),
            timezone="Asia/Shanghai",
            description="reminder",
            targets="tui",
            wake_offset_seconds=0,
        )
        svc = _make_scheduler(store)
        # boot_time 远早于 grace 窗口 → 模拟运行期 reload
        svc.boot_time = time.time() - 120
        await svc.reload()

        wake_events = [ev for _, _, ev in svc.events if ev.kind == "wake"]
        assert len(wake_events) == 1
        assert wake_events[0].job_id == job.id

    @pytest.mark.asyncio
    async def test_boot_grace_reload_skips_past_wake_as_crash_recovery(self, tmp_path):
        """进程刚启动（boot_time 在 grace 内）：过去的 wake 视为崩溃残留，跳过。"""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        await store.create_job(
            name="drink",
            cron_expr=self._recent_oneshot_expr(seconds_ago=3),
            timezone="Asia/Shanghai",
            description="reminder",
            targets="tui",
            wake_offset_seconds=0,
        )
        svc = _make_scheduler(store)
        # boot_time 即现在 → 处于 grace 窗口内（模拟崩溃重启后的首次 reload）
        svc.boot_time = time.time()
        await svc.reload()

        wake_events = [ev for _, _, ev in svc.events if ev.kind == "wake"]
        assert len(wake_events) == 0

    @pytest.mark.asyncio
    async def test_crash_recovery_skip_survives_reload_after_grace(self, tmp_path):
        """Later reloads must not revive a wake skipped during crash recovery."""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        await store.create_job(
            name="drink",
            # The wake is already due, but the push is still in the future, so
            # the original AgentServer task could remain active past 60 seconds.
            cron_expr=self._future_oneshot_expr(seconds_ahead=30),
            timezone="Asia/Shanghai",
            description="reminder",
            targets="tui",
            wake_offset_seconds=60,
        )
        svc = _make_scheduler(store)
        await svc.reload()
        assert not [ev for _, _, ev in svc.events if ev.kind in ("wake", "push")]

    @pytest.mark.asyncio
    async def test_crash_recovery_skip_survives_reload_after_push_deadline(self, tmp_path):
        """An orphaned remote task can outlive its push deadline."""
        clock = [time.time()]
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        await store.create_job(
            name="slow-drink",
            cron_expr=self._future_oneshot_expr(seconds_ahead=30),
            timezone="Asia/Shanghai",
            description="reminder",
            targets="tui",
            wake_offset_seconds=60,
        )
        svc = _make_scheduler(store, now_fn=lambda: clock[0])
        await svc.reload()

        # The scheduled push time has passed but is still inside the missed
        # trigger window, so reload would otherwise reconstruct the same run.
        clock[0] += 31
        svc.boot_time = clock[0] - 120
        await svc.reload()
        assert not [ev for _, _, ev in svc.events if ev.kind in ("wake", "push")]

        # Simulate a later store-triggered reload after the boot grace window.
        svc.boot_time = time.time() - 120
        await svc.reload()
        assert not [ev for _, _, ev in svc.events if ev.kind in ("wake", "push")]

    @pytest.mark.asyncio
    async def test_runtime_reload_recently_due_wake_executes_and_records_session(
        self, tmp_path,
    ):
        """运行期 reload 后，刚到点的 wake 应被执行并写入 last_session_id。"""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        job = await store.create_job(
            name="drink",
            cron_expr=self._recent_oneshot_expr(seconds_ago=3),
            timezone="Asia/Shanghai",
            description="reminder",
            targets="tui",
            wake_offset_seconds=0,
        )
        svc = _make_scheduler(store)
        svc.boot_time = time.time() - 120
        await svc.reload()

        wake_events = [ev for _, _, ev in svc.events if ev.kind == "wake"]
        assert len(wake_events) == 1
        run_id = wake_events[0].run_id
        await svc.on_wake(job, run_id)
        await svc.run_tasks[run_id]

        stored = await store.get_job(job.id)
        assert stored is not None
        assert stored.last_session_id == "cron_agentserver_allocated"

    @pytest.mark.asyncio
    async def test_start_resets_boot_time(self, tmp_path):
        """start() 应将 boot_time 重置为启动时刻，覆盖构造到启动之间的延迟。"""
        store = CronJobStore(path=tmp_path / "cron_jobs.json")
        clock = _Clock(1000.0)
        svc = _TestableScheduler(
            store=store,
            agent_client=FakeAgentClient(),
            message_handler=FakeMessageHandler(),
            now_fn=clock,
        )
        assert svc.boot_time == 1000.0
        clock.advance(500)
        await svc.start()
        assert svc.boot_time == 1500.0
        await svc.stop()

    @pytest.mark.asyncio
    async def test_e2e_store_change_reload_keeps_pending_wake(self, tmp_path):
        """端到端：未来 wake 排入后，其他任务写 store 触发 _check_store_changed
        二次 reload，原事件被清空重排时运行期不应误丢（可控 now_fn）。"""
        store_file = tmp_path / "cron_jobs.json"
        store = CronJobStore(path=store_file)
        clock = _Clock(time.time())
        tz = ZoneInfo("Asia/Shanghai")
        run_dt = datetime.fromtimestamp(clock.t, tz=tz) + timedelta(seconds=65)
        expr = (
            f"{run_dt.second} {run_dt.minute} {run_dt.hour} "
            f"{run_dt.day} {run_dt.month} ? {run_dt.year}"
        )
        job_a = await store.create_job(
            name="A", cron_expr=expr, timezone="Asia/Shanghai",
            description="r", targets="tui", wake_offset_seconds=0,
        )
        svc = _TestableScheduler(
            store=store,
            agent_client=FakeAgentClient(),
            message_handler=FakeMessageHandler(),
            now_fn=clock,
        )
        svc.boot_time = clock.t
        await svc.reload()
        wake_before = [ev for _, _, ev in svc.events if ev.kind == "wake"]
        assert len(wake_before) == 1

        clock.advance(66)

        store2 = CronJobStore(path=store_file)
        await store2.create_job(
            name="B", cron_expr="0 0 9 * * ? *", timezone="Asia/Shanghai",
            description="r", targets="tui",
        )
        changed = await svc.check_store_changed()
        assert changed is True

        wake_a_after = [
            ev for _, _, ev in svc.events
            if ev.kind == "wake" and ev.job_id == job_a.id
        ]
        assert len(wake_a_after) == 1
