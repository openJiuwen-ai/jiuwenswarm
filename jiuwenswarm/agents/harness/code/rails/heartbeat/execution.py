# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Heartbeat admission and AgentServer-local execution."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from jiuwenswarm.common.schema.agent import AgentRequest

from .models import HeartbeatJob

logger = logging.getLogger(__name__)

DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300.0
DEFAULT_USER_PREEMPTION_TIMEOUT_SECONDS = 10.0


def _consume_background_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("[HeartbeatAdmission] background preemption failed")


@dataclass
class _SessionAdmissionState:
    active_users: int = 0
    user_waiters: int = 0
    team_user_submissions: int = 0
    team_user_active: bool = False
    heartbeat_run_id: str | None = None
    heartbeat_blocked: bool = False


class SessionRunAdmission:
    """Narrow arbitration layer shared by user turns and Heartbeat runs.

    It does not replace the existing runtime queues. It only prevents a
    Heartbeat from entering the actual runtime while a user turn is active or
    waiting, and prevents a new user turn from racing an already admitted
    Heartbeat.
    """

    def __init__(
        self,
        *,
        user_preemption_timeout_seconds: float = (
            DEFAULT_USER_PREEMPTION_TIMEOUT_SECONDS
        ),
    ) -> None:
        self._condition = asyncio.Condition()
        self._states: dict[str, _SessionAdmissionState] = {}
        self._heartbeat_preemptor: Callable[[str], Awaitable[bool]] | None = None
        self._user_preemption_timeout_seconds = max(
            0.001,
            float(user_preemption_timeout_seconds),
        )

    def set_heartbeat_preemptor(
        self,
        preemptor: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Attach the execution owner used to cancel an active Heartbeat."""
        self._heartbeat_preemptor = preemptor

    def _state(self, session_id: str) -> _SessionAdmissionState:
        return self._states.setdefault(session_id, _SessionAdmissionState())

    def is_user_active(self, session_id: str) -> bool:
        state = self._states.get(session_id)
        if state is None:
            return False
        direct_user_work = bool(state.active_users or state.user_waiters)
        team_user_work = bool(
            state.team_user_submissions or state.team_user_active
        )
        return direct_user_work or team_user_work

    def active_heartbeat_sessions(self) -> set[str]:
        return {
            session_id
            for session_id, state in self._states.items()
            if state.heartbeat_run_id is not None
        }

    def is_heartbeat_active(
        self, session_id: str, *, exclude_run_id: str = ""
    ) -> bool:
        state = self._states.get(session_id)
        if state is None or state.heartbeat_run_id is None:
            return False
        return state.heartbeat_run_id != str(exclude_run_id or "")

    async def block_heartbeats(self, session_id: str) -> str | None:
        """Prevent new Heartbeats while a Session deletion is prepared."""
        async with self._condition:
            state = self._state(session_id)
            state.heartbeat_blocked = True
            return state.heartbeat_run_id

    async def unblock_heartbeats(self, session_id: str) -> None:
        async with self._condition:
            state = self._states.get(session_id)
            if state is None:
                return
            state.heartbeat_blocked = False
            self._drop_idle_state(session_id, state)
            self._condition.notify_all()

    async def _preempt_heartbeat_for_user(
        self,
        session_id: str,
        run_id: str | None,
    ) -> None:
        preemptor = self._heartbeat_preemptor
        if not run_id or preemptor is None:
            return
        logger.info(
            "[HeartbeatAdmission] user request preempting heartbeat: "
            "session=%s run=%s",
            session_id,
            run_id,
        )
        preemption_task = asyncio.create_task(
            preemptor(run_id),
            name=f"heartbeat-user-preempt-{run_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {preemption_task},
                timeout=self._user_preemption_timeout_seconds,
            )
        except BaseException:
            preemption_task.add_done_callback(_consume_background_task_result)
            preemption_task.cancel()
            raise
        if not done:
            timeout = self._user_preemption_timeout_seconds
            logger.error(
                "[HeartbeatAdmission] heartbeat preemption timed out: "
                "session=%s run=%s timeout=%.3fs",
                session_id,
                run_id,
                timeout,
            )
            preemption_task.add_done_callback(_consume_background_task_result)
            preemption_task.cancel()
            raise RuntimeError(
                f"heartbeat preemption timed out after {timeout:g} seconds"
            )
        cancelled = preemption_task.result()

        if cancelled:
            return
        # Another concurrent user request may already be cancelling this run.
        # Wait for that exact cleanup instead of failing one of the users.
        try:
            async with asyncio.timeout(self._user_preemption_timeout_seconds):
                async with self._condition:
                    await self._condition.wait_for(
                        lambda: (
                            self._states.get(session_id) is None
                            or self._states[session_id].heartbeat_run_id != run_id
                        )
                    )
        except TimeoutError as exc:
            raise RuntimeError(
                f"active heartbeat {run_id} could not be preempted"
            ) from exc

    async def _begin_interactive_user(
        self,
        session_id: str,
        *,
        team: bool,
    ) -> None:
        async with self._condition:
            state = self._state(session_id)
            state.user_waiters += 1
            heartbeat_run_id = state.heartbeat_run_id
        try:
            await self._preempt_heartbeat_for_user(session_id, heartbeat_run_id)
            async with self._condition:
                await self._condition.wait_for(
                    lambda: (
                        self._state(session_id).heartbeat_run_id is None
                        and (not team or self._state(session_id).active_users == 0)
                    )
                )
                state = self._state(session_id)
                if team:
                    state.team_user_submissions += 1
                else:
                    state.active_users += 1
        finally:
            async with self._condition:
                state = self._states.get(session_id)
                if state is not None:
                    state.user_waiters -= 1
                    self._drop_idle_state(session_id, state)
                    self._condition.notify_all()

    async def begin_user(self, session_id: str) -> None:
        await self._begin_interactive_user(session_id, team=False)

    async def begin_team_user(self, session_id: str) -> None:
        """Mark an interactive Team iteration without serializing its steers.

        Team decides whether an input is an immediate steer or a follow-up.
        Admission only keeps Heartbeat outside the active user iteration.
        Multiple inputs therefore register concurrent pending submissions and
        never wait for one another.
        """
        await self._begin_interactive_user(session_id, team=True)

    async def complete_team_user_submission(
        self,
        session_id: str,
        *,
        accepted: bool,
    ) -> None:
        """Resolve one steer submission and retain activity only if accepted."""
        async with self._condition:
            state = self._states.get(session_id)
            if state is None:
                return
            state.team_user_submissions = max(0, state.team_user_submissions - 1)
            if accepted:
                state.team_user_active = True
            self._drop_idle_state(session_id, state)
            self._condition.notify_all()

    async def begin_bounded_team_user(self, session_id: str) -> None:
        """Exclusively admit bounded Cron automation around Team activity."""
        async with self._condition:
            state = self._state(session_id)
            state.user_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: self._bounded_team_user_can_begin(session_id)
                )
                state.active_users += 1
            finally:
                state.user_waiters -= 1

    def _bounded_team_user_can_begin(self, session_id: str) -> bool:
        state = self._state(session_id)
        direct_user_active = state.active_users > 0
        team_user_active = bool(
            state.team_user_submissions or state.team_user_active
        )
        return (
            state.heartbeat_run_id is None
            and not direct_user_active
            and not team_user_active
        )

    async def end_team_user(self, session_id: str) -> None:
        """Release the interactive Team marker at its runtime terminal event."""
        async with self._condition:
            state = self._states.get(session_id)
            if state is None:
                return
            state.team_user_active = False
            self._drop_idle_state(session_id, state)
            self._condition.notify_all()

    async def end_user(self, session_id: str) -> None:
        async with self._condition:
            state = self._states.get(session_id)
            if state is None:
                return
            state.active_users = max(0, state.active_users - 1)
            self._drop_idle_state(session_id, state)
            self._condition.notify_all()

    async def try_begin_heartbeat(self, session_id: str, run_id: str) -> bool:
        async with self._condition:
            state = self._state(session_id)
            direct_user_work = bool(state.active_users or state.user_waiters)
            team_user_work = bool(
                state.team_user_submissions or state.team_user_active
            )
            user_has_work = direct_user_work or team_user_work
            session_has_work = user_has_work or state.heartbeat_run_id is not None
            if state.heartbeat_blocked or session_has_work:
                return False
            state.heartbeat_run_id = run_id
            return True

    async def end_heartbeat(self, session_id: str, run_id: str) -> None:
        async with self._condition:
            state = self._states.get(session_id)
            if state is None or state.heartbeat_run_id != run_id:
                return
            state.heartbeat_run_id = None
            self._drop_idle_state(session_id, state)
            self._condition.notify_all()

    def _drop_idle_state(
        self, session_id: str, state: _SessionAdmissionState
    ) -> None:
        direct_user_work = bool(state.active_users or state.user_waiters)
        team_user_work = bool(
            state.team_user_submissions or state.team_user_active
        )
        user_has_work = direct_user_work or team_user_work
        session_has_work = user_has_work or state.heartbeat_run_id is not None
        if not session_has_work and not state.heartbeat_blocked:
            self._states.pop(session_id, None)


class _SchedulerCallback(Protocol):
    async def on_run_finished(
        self,
        job_id: str,
        run_id: str,
        *,
        outcome: str,
        error: str | None = None,
        pause_schedule: bool = False,
        consume_queue: bool = True,
    ) -> bool:
        ...


class HeartbeatExecutionService:
    """Run claimed Heartbeat jobs through the AgentServer's normal agent path."""

    def __init__(
        self,
        server: Any,
        admission: SessionRunAdmission,
        *,
        execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    ) -> None:
        self._server = server
        self._admission = admission
        self._execution_timeout_seconds = max(
            0.001,
            float(execution_timeout_seconds),
        )
        self._scheduler: _SchedulerCallback | None = None
        self._completion_hook: Callable[[str], Awaitable[None]] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._jobs: dict[str, HeartbeatJob] = {}
        self._user_preempted_runs: set[str] = set()
        self._admission.set_heartbeat_preemptor(self.preempt_for_user)

    def set_scheduler(self, scheduler: _SchedulerCallback) -> None:
        self._scheduler = scheduler

    def set_completion_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        self._completion_hook = hook

    def is_session_busy(self, session_id: str, *, exclude_run_id: str = "") -> bool:
        return self._admission.is_user_active(
            session_id
        ) or self._admission.is_heartbeat_active(
            session_id,
            exclude_run_id=exclude_run_id,
        )

    def has_active_run(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def active_session_ids(self) -> set[str]:
        return self._admission.active_heartbeat_sessions()

    async def dispatch(
        self,
        job: HeartbeatJob,
        run_id: str,
        request_message: Any,
    ) -> bool:
        """Atomically admit and start a run; return False on a busy race."""
        if not await self._admission.try_begin_heartbeat(job.session_id, run_id):
            return False
        task = asyncio.create_task(
            self._run(job, run_id, request_message),
            name=f"heartbeat-run-{run_id}",
        )
        self._tasks[run_id] = task
        self._jobs[run_id] = job
        return True

    async def _run(
        self, job: HeartbeatJob, run_id: str, request_message: Any
    ) -> None:
        outcome = "succeeded"
        error: str | None = None
        try:
            request = AgentRequest(
                request_id=run_id,
                channel_id=str(request_message.channel_id or job.channel_id),
                session_id=job.session_id,
                chat_id=request_message.chat_id,
                req_method=request_message.req_method,
                params=dict(request_message.params or {}),
                is_stream=True,
                timestamp=float(request_message.timestamp or 0.0),
                metadata=dict(request_message.metadata or {}),
                user_id=str(request_message.user_id or ""),
                agent_ref=request_message.agent_ref,
            )
            execution_deadline = asyncio.timeout(self._execution_timeout_seconds)
            try:
                async with execution_deadline:
                    await self._server.execute_internal_heartbeat(request)
            except TimeoutError:
                if not execution_deadline.expired():
                    raise
                outcome = "failed"
                timeout = self._execution_timeout_seconds
                error = f"heartbeat execution timed out after {timeout:g} seconds"
                logger.error(
                    "[HeartbeatExecution] run timed out: "
                    "job=%s run=%s timeout=%.3fs",
                    job.id,
                    run_id,
                    timeout,
                )
        except asyncio.CancelledError:
            outcome = "cancelled"
            if run_id in self._user_preempted_runs:
                error = "heartbeat preempted by user request"
        except Exception as exc:  # noqa: BLE001
            outcome = "failed"
            error = str(exc)
            logger.exception(
                "[HeartbeatExecution] run failed: job=%s run=%s", job.id, run_id
            )
        finally:
            await self._finish_run(job, run_id, outcome=outcome, error=error)

    async def _finish_run(
        self,
        job: HeartbeatJob,
        run_id: str,
        *,
        outcome: str,
        error: str | None,
    ) -> None:
        self._tasks.pop(run_id, None)
        self._jobs.pop(run_id, None)
        try:
            await self._admission.end_heartbeat(job.session_id, run_id)
            if self._scheduler is not None:
                await self._scheduler.on_run_finished(
                    job.id,
                    run_id,
                    outcome=outcome,
                    error=error,
                )
            if self._completion_hook is not None:
                try:
                    await self._completion_hook(job.session_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[HeartbeatExecution] completion hook failed: session=%s",
                        job.session_id,
                    )
        finally:
            self._user_preempted_runs.discard(run_id)

    async def cancel(self, run_id: str, *, reason: str = "") -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        if reason == "user_request":
            self._user_preempted_runs.add(run_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Cancellation before the coroutine's first step bypasses _run.finally.
        if self._tasks.get(run_id) is task:
            job = self._jobs.get(run_id)
            if job is not None:
                error = (
                    "heartbeat preempted by user request"
                    if reason == "user_request"
                    else None
                )
                await self._finish_run(
                    job,
                    run_id,
                    outcome="cancelled",
                    error=error,
                )
        return True

    async def preempt_for_user(self, run_id: str) -> bool:
        return await self.cancel(run_id, reason="user_request")

    async def stop(self) -> None:
        runs = list(self._tasks.items())
        tasks = [task for _, task in runs]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run_id, task in runs:
            if self._tasks.get(run_id) is not task:
                continue
            job = self._jobs.get(run_id)
            if job is not None:
                await self._finish_run(
                    job,
                    run_id,
                    outcome="cancelled",
                    error=None,
                )
        self._tasks.clear()
        self._jobs.clear()


__all__ = [
    "DEFAULT_EXECUTION_TIMEOUT_SECONDS",
    "DEFAULT_USER_PREEMPTION_TIMEOUT_SECONDS",
    "HeartbeatExecutionService",
    "SessionRunAdmission",
]
