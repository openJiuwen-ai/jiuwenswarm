"""Lifecycle of the JiuwenMate runtime: ports, scheduler and periodic window cleanup.

``AgentManager`` assembles the host ports shared by every Execution, owns the
``SessionScheduler`` and runs window cleanup in the background. It is started
and closed together with its host; deleting a colleague goes through it so the
colleague's work has ended before its data is removed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.execution_driver import (
    AbortEntry,
    ExecuteEntry,
    ExecutionDriver,
    _default_abort,
    _default_execute,
)
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.ports.budget import DEFAULT_RESERVATION_TOKENS, HostBudgetPort
from jiuwenswarm.server.jiuwenmate.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.session_scheduler import SchedulerLimits, SessionScheduler

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_RETENTION = timedelta(days=90)
DEFAULT_PURGE_INTERVAL = timedelta(hours=1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentManager:
    def __init__(
        self,
        service: PersistentAgentService,
        sessions: ExecutionRepository,
        windows: WindowBackendStore,
        budgets: BudgetRepository,
        *,
        execute: ExecuteEntry = _default_execute,
        abort: AbortEntry = _default_abort,
        limits: SchedulerLimits | None = None,
        reservation_tokens: int = DEFAULT_RESERVATION_TOKENS,
        archive_retention: timedelta = DEFAULT_ARCHIVE_RETENTION,
        purge_interval: timedelta = DEFAULT_PURGE_INTERVAL,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if purge_interval <= timedelta(0):
            raise ValueError("purge_interval must be positive")
        if archive_retention < timedelta(0):
            raise ValueError("archive_retention must not be negative")
        self._service = service
        self._windows = windows
        self._archive_retention = archive_retention
        self._purge_interval = purge_interval
        self._queue = CommitQueue(sessions, clock=clock)
        driver = ExecutionDriver(
            snapshots=service.snapshot,
            commit_port=HostCommitPort(sessions, self._queue, clock=clock),
            queue=self._queue,
            budget_port=HostBudgetPort(budgets, sessions, reservation_tokens=reservation_tokens, clock=clock),
            budgets=budgets,
            windows=windows,
            execute=execute,
            abort=abort,
            clock=clock,
        )
        self._scheduler = SessionScheduler(sessions, driver, limits=limits, clock=clock)
        self._purge_task: asyncio.Task[None] | None = None

    @property
    def scheduler(self) -> SessionScheduler:
        return self._scheduler

    @property
    def commit_queue(self) -> CommitQueue:
        return self._queue

    async def start(self) -> None:
        if self._purge_task is not None:
            return
        await self._queue.start()
        self._scheduler.start()
        self._purge_task = asyncio.create_task(self._purge_loop(), name="jiuwenmate-window-purge")

    async def aclose(self) -> None:
        """Stop every Execution, wait for their streams to end, then stop cleanup and the commit queue."""
        await self._scheduler.aclose()
        if self._purge_task is not None:
            self._purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._purge_task
            self._purge_task = None
        await self._queue.aclose()

    async def delete_colleague(self, namespace_id: str, agent_id: str) -> None:
        """End the colleague's queued and running work, then remove the colleague and its window data."""
        await self._scheduler.end_agent(namespace_id, agent_id)
        try:
            await asyncio.to_thread(self._service.delete_colleague, namespace_id, agent_id)
        finally:
            self._scheduler.release_agent(namespace_id, agent_id)

    async def _purge_loop(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(
                    self._windows.purge_expired, archive_retention=self._archive_retention
                )
                if removed:
                    logger.info("[JiuwenMate] window entries purged: %s", removed)
            except Exception as exc:
                logger.warning("[JiuwenMate] window purge failed: %s", exc)
            await asyncio.sleep(self._purge_interval.total_seconds())
