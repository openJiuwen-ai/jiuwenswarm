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


@dataclass
class _SessionAdmissionState:
    active_users: int = 0
    user_waiters: int = 0
    heartbeat_run_id: str | None = None
    heartbeat_blocked: bool = False


class SessionRunAdmission:
    """Narrow arbitration layer shared by user turns and Heartbeat runs.

    It does not replace the existing runtime queues. It only prevents a
    Heartbeat from entering the actual runtime while a user turn is active or
    waiting, and prevents a new user turn from racing an already admitted
    Heartbeat.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._states: dict[str, _SessionAdmissionState] = {}

    def _state(self, session_id: str) -> _SessionAdmissionState:
        return self._states.setdefault(session_id, _SessionAdmissionState())

    def is_user_active(self, session_id: str) -> bool:
        state = self._states.get(session_id)
        return bool(state and (state.active_users or state.user_waiters))

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

    async def begin_user(self, session_id: str) -> None:
        async with self._condition:
            state = self._state(session_id)
            state.user_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: self._state(session_id).heartbeat_run_id is None
                )
                state.active_users += 1
            finally:
                state.user_waiters -= 1

    async def begin_team_user(self, session_id: str) -> None:
        """Exclusively admit one Team round and keep Heartbeat behind it.

        A Team runner stream can outlive many interaction rounds.  The caller
        must pair this method with ``end_user`` only after the submitted round
        reaches a terminal event, not when the transport coroutine returns.
        """
        async with self._condition:
            state = self._state(session_id)
            state.user_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: (
                        self._state(session_id).heartbeat_run_id is None
                        and self._state(session_id).active_users == 0
                    )
                )
                state.active_users += 1
            finally:
                state.user_waiters -= 1

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
            session_has_work = bool(
                state.active_users or state.user_waiters or state.heartbeat_run_id
            )
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
        session_has_work = bool(
            state.active_users or state.user_waiters or state.heartbeat_run_id
        )
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

    def __init__(self, server: Any, admission: SessionRunAdmission) -> None:
        self._server = server
        self._admission = admission
        self._scheduler: _SchedulerCallback | None = None
        self._completion_hook: Callable[[str], Awaitable[None]] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}

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
            await self._server.execute_internal_heartbeat(request)
        except asyncio.CancelledError:
            outcome = "cancelled"
        except Exception as exc:  # noqa: BLE001
            outcome = "failed"
            error = str(exc)
            logger.exception(
                "[HeartbeatExecution] run failed: job=%s run=%s", job.id, run_id
            )
        finally:
            self._tasks.pop(run_id, None)
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

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


__all__ = ["HeartbeatExecutionService", "SessionRunAdmission"]
