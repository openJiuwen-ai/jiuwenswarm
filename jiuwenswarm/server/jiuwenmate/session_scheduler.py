"""Dispatch queued Requests: one at a time per Session, many Sessions within capacity.

The Request queue and its dispatch state live in ``ExecutionRepository``; this
scheduler only tracks what this process is running. A Session's next Request is
dispatched only after the driver has returned, that is, after core's stream has
ended and its instance is released. Controls reach only their target Request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from jiuwenswarm.server.jiuwenmate.execution_driver import AbortReason, ExecutionDriver, ExecutionHandle
from jiuwenswarm.server.jiuwenmate.execution_models import RequestState, SessionRecord
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

logger = logging.getLogger(__name__)

SessionKey = tuple[str, str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SchedulerLimits:
    """Placeholder capacity until real load is measured (S1)."""

    max_running: int = 4
    max_running_per_agent: int = 2
    max_queued_per_session: int = 100

    def __post_init__(self) -> None:
        for name in ("max_running", "max_running_per_agent", "max_queued_per_session"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass
class _Running:
    state: RequestState
    handle: ExecutionHandle
    task: asyncio.Task[None] | None = None


class SessionScheduler:
    def __init__(
        self,
        sessions: ExecutionRepository,
        driver: ExecutionDriver,
        *,
        limits: SchedulerLimits | None = None,
        clock: Callable[[], datetime] = _utc_now,
        has_pending_approval: Callable[[str, str], bool] | None = None,
        is_ready_approval: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        self._sessions = sessions
        self._driver = driver
        self._limits = limits or SchedulerLimits()
        self._clock = clock
        self._has_pending_approval = has_pending_approval
        self._is_ready_approval = is_ready_approval
        self._lock = asyncio.Lock()
        self._running: dict[SessionKey, _Running] = {}
        # Sessions that may still have queued Requests, in the order they got work.
        self._waiting: dict[SessionKey, None] = {}
        self._deleting: set[tuple[str, str]] = set()
        self._open = False

    def start(self) -> None:
        self._open = True

    @property
    def running(self) -> list[RequestState]:
        return [run.state for run in self._running.values()]

    def _require_open(self, namespace_id: str, agent_id: str) -> None:
        if not self._open:
            raise PersistentAgentError("scheduler is not running", code="SCHEDULER_CLOSED")
        if (namespace_id, agent_id) in self._deleting:
            raise PersistentAgentError("colleague is being deleted", code="AGENT_DELETING")

    async def accept(self, session: SessionRecord, **accept_kwargs: Any) -> tuple[RequestState, bool]:
        """Queue a new Request through ``ExecutionRepository.accept_request`` within the Session's queue limit."""
        self._require_open(session.namespace_id, session.agent_id)
        async with self._lock:
            states = await asyncio.to_thread(self._sessions.session_requests, session.namespace_id, session.session_id)
            if sum(state.status == "queued" for state in states) >= self._limits.max_queued_per_session:
                raise PersistentAgentError("session queue is full", code="QUEUE_FULL")
            accepted = await asyncio.to_thread(self._sessions.accept_request, session, **accept_kwargs)
            self._waiting[(session.namespace_id, session.session_id)] = None
        await self._pump()
        return accepted

    def resume(self, state: RequestState) -> None:
        """Queue one paused Request again when its interruption is still awaiting continuation.

        The caller pumps the scheduler afterwards. A stop, a cancel, a saved final
        and a confirmation pause are not continued here.
        """
        record = state.record
        self._require_open(record.namespace_id, record.agent_id)
        opened = self._sessions.reopen_for_continuation(record.namespace_id, record.request_id, now=self._clock())
        if not opened:
            raise PersistentAgentError("request is not awaiting continuation", code="CONTINUATION_NOT_READY")

    async def recover(
        self,
        namespace_id: str,
        *,
        channels: frozenset[str] | None = None,
        include_unrouted: bool = True,
    ) -> list[RequestState]:
        """Schedule queued Requests again; return dispatched ones left by a previous process.

        ``channels`` limits recovery to routes this runtime can finish. An unrouted
        Request stays with the caller that sets ``include_unrouted``.
        """
        pending = await asyncio.to_thread(self._sessions.pending_requests, namespace_id)
        pending = [state for state in pending if _route_selected(state, channels, include_unrouted)]
        running = {run.state.record.request_id for run in self._running.values()}
        left = [state for state in pending if state.status == "dispatched" and state.record.request_id not in running]
        for state in pending:
            if state.status == "queued":
                self._waiting[(namespace_id, state.record.session_id)] = None
        await self._pump()
        return left

    async def wake(self, namespace_id: str, session_id: str) -> None:
        """Schedule the Session again, for example once a Request it was blocked on has been closed."""
        async with self._lock:
            self._waiting[(namespace_id, session_id)] = None
        await self._pump()

    def _run_of(self, namespace_id: str, request_id: str) -> _Running | None:
        return next(
            (
                run
                for (ns, _), run in self._running.items()
                if ns == namespace_id and run.state.record.request_id == request_id
            ),
            None,
        )

    async def abort(self, namespace_id: str, request_id: str, reason: AbortReason) -> None:
        """End only the target Request: interrupt it if it runs here, otherwise close it as stopped or cancelled.

        A Request left dispatched by a previous process is closed too, and its Session's queue moves on.
        """
        async with self._lock:
            run = self._run_of(namespace_id, request_id)
            if run is None:
                state = await asyncio.to_thread(self._sessions.get_request, namespace_id, request_id)
                if state.status != "ended":
                    outcome = "cancelled" if reason == "cancel" else "stopped"
                    await asyncio.to_thread(
                        self._sessions.finish_request, namespace_id, request_id, outcome, now=self._clock()
                    )
                    self._waiting[(namespace_id, state.record.session_id)] = None
        if run is not None:
            await run.handle.abort(reason)
        else:
            await self._pump()

    async def end_agent(self, namespace_id: str, agent_id: str) -> None:
        """Refuse new Requests of the colleague, cancel its queued ones and wait for its running ones."""
        self._deleting.add((namespace_id, agent_id))
        async with self._lock:
            pending = await asyncio.to_thread(self._sessions.pending_requests, namespace_id)
            for state in pending:
                if state.record.agent_id == agent_id and state.status == "queued":
                    await asyncio.to_thread(
                        self._sessions.finish_request,
                        namespace_id,
                        state.record.request_id,
                        "cancelled",
                        now=self._clock(),
                    )
        runs = [
            run
            for (ns, _), run in self._running.items()
            if ns == namespace_id and run.state.record.agent_id == agent_id
        ]
        for run in runs:
            await run.handle.abort("cancel")
        await asyncio.gather(*(run.task for run in runs if run.task is not None), return_exceptions=True)

    def release_agent(self, namespace_id: str, agent_id: str) -> None:
        self._deleting.discard((namespace_id, agent_id))

    async def drain(self) -> None:
        """Wait until nothing is running, including Requests dispatched meanwhile."""
        while self._running:
            tasks = [run.task for run in self._running.values() if run.task is not None]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop accepting, stop every running Execution and wait until each stream has ended."""
        self._open = False
        for run in list(self._running.values()):
            await run.handle.abort("stop")
        await self.drain()

    def _agent_running(self, namespace_id: str, agent_id: str) -> int:
        return sum(
            1 for (ns, _), run in self._running.items() if ns == namespace_id and run.state.record.agent_id == agent_id
        )

    async def _pump(self) -> None:
        async with self._lock:
            for key in list(self._waiting):
                if not self._open or len(self._running) >= self._limits.max_running:
                    return
                if key in self._running:
                    continue
                states = await asyncio.to_thread(self._sessions.session_requests, *key)
                if any(state.status == "dispatched" for state in states):
                    # Left dispatched by a previous process; JM-13 decides how it continues.
                    self._waiting.pop(key, None)
                    continue
                head = next((state for state in states if state.status == "queued"), None)
                if head is None:
                    self._waiting.pop(key, None)
                    continue
                if self._has_pending_approval is not None and await asyncio.to_thread(
                    self._has_pending_approval, key[0], key[1]
                ):
                    ready_continuation = (
                        self._is_ready_approval is not None
                        and await asyncio.to_thread(
                            self._is_ready_approval, key[0], key[1], head.record.request_id
                        )
                    )
                    if not ready_continuation:
                        continue
                agent_id = head.record.agent_id
                if (key[0], agent_id) in self._deleting:
                    continue
                if self._agent_running(key[0], agent_id) >= self._limits.max_running_per_agent:
                    continue
                execution = await asyncio.to_thread(
                    self._sessions.start_execution, key[0], head.record.request_id, now=self._clock()
                )
                run = _Running(head, self._driver.new_handle())
                self._running[key] = run
                run.task = asyncio.create_task(self._execute(key, run, execution), name="jiuwenmate-execution")

    async def _execute(self, key: SessionKey, run: _Running, execution) -> None:
        record = run.state.record
        try:
            outcome = await self._driver.run(run.state, execution, run.handle)
            try:
                await asyncio.to_thread(
                    self._sessions.finish_request, record.namespace_id, record.request_id, outcome, now=self._clock()
                )
            except Exception as exc:
                logger.warning(
                    "[JiuwenMate] request outcome not saved: request=%s outcome=%s error=%s",
                    record.request_id,
                    outcome,
                    exc,
                )
        finally:
            self._running.pop(key, None)
        await self._pump()


def _route_selected(state: RequestState, channels: frozenset[str] | None, include_unrouted: bool) -> bool:
    if channels is None:
        return True
    route = state.record.route_snapshot
    channel = None if route is None else route.channel
    if channel is None:
        return include_unrouted
    return channel in channels
