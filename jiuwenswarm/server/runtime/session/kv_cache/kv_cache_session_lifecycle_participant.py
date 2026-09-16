# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""KVC product implementation of Runtime-owned Session lifecycle protocols."""

from __future__ import annotations

import asyncio
import logging

from openjiuwen.core.kv_cache.kv_cache_config import (
    KVC_TERMINAL_CLEANUP_TIMEOUT_SECONDS,
)

from jiuwenswarm.runtime.session_lifecycle import (
    SessionExecutionEvent,
    SessionExecutionFinishedEvent,
    SessionForegroundEvent,
    SessionInactiveEvent,
    SessionKind,
    SessionLifecycleTarget,
    SessionPrepareDisposition,
    SessionPrepareEvent,
)
from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_task_guard


_LOGGER = logging.getLogger(__name__)


class KVCacheSessionLifecycleParticipant:
    """Translate Runtime lifecycle events into best-effort KVC operations.

    Runtime owns the business lifecycle and invokes this optional participant.
    This class may maintain or release derived remote cache state, but it must
    never delete Session metadata, messages, files, or Team business state.
    """

    def __init__(self, runtime: object) -> None:
        self._runtime = runtime
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def name(self) -> str:
        return "kv_cache"

    async def before_delete(self, target: SessionLifecycleTarget) -> None:
        # Install a local tombstone before the destructive boundary so a late
        # foreground/task event cannot schedule a new prefetch or offload.
        # The actual terminal eviction belongs to release_resources(), where
        # Agent Core preserves its release ordering and await semantics.
        self._guard().delete(
            session_id=target.descriptor.session_id,
            is_team=target.kind is SessionKind.TEAM,
        )

    async def release_resources(self, target: SessionLifecycleTarget) -> None:
        # Keep Session construction lazy: when KVC is disabled this participant
        # is never installed, so the OFF path does not load Agent Core's KVC
        # session implementation or perform KVC-derived work.
        from openjiuwen.core.session.agent import create_agent_session
        from openjiuwen.core.session.agent_team import create_agent_team_session

        factory = (
            create_agent_team_session
            if target.kind is SessionKind.TEAM
            else create_agent_session
        )
        session = factory(
            session_id=target.descriptor.session_id,
            kv_cache_runtime=self._runtime,
        )
        async with asyncio.timeout(KVC_TERMINAL_CLEANUP_TIMEOUT_SECONDS):
            await session.release_kvc()

    async def delete_failed(
        self,
        target: SessionLifecycleTarget,
        *,
        destructive_started: bool,
    ) -> None:
        if destructive_started:
            # Once business deletion has begun, restoring the tombstone could
            # allow stale activity to repopulate cache for a dying Session.
            return
        self._guard().restore_after_failed_delete(target.descriptor.session_id)

    async def delete_committed(self, target: SessionLifecycleTarget) -> None:
        # The Session is gone, so its process-local deduplication facts can be
        # discarded instead of growing for the lifetime of the process.
        self._guard().forget(target.descriptor.session_id)

    async def session_preparing(
        self,
        event: SessionPrepareEvent,
    ) -> SessionPrepareDisposition:
        guard = self._guard()
        target = event.target
        guard.set_foreground(
            session_id=target.descriptor.session_id,
            view_id=event.view_id or "default-view",
            visible=True,
            is_team=target.kind is SessionKind.TEAM,
            has_history=event.has_history,
        )
        action = guard.prepare(
            session_id=target.descriptor.session_id,
            intent_id=event.intent_id,
            is_team=target.kind is SessionKind.TEAM,
            has_history=event.has_history,
        )
        self._schedule(action)
        return (
            SessionPrepareDisposition.SCHEDULED
            if action is not None
            else SessionPrepareDisposition.NOT_NEEDED
        )

    async def foreground_changed(self, event: SessionForegroundEvent) -> None:
        target = event.target
        action = self._guard().set_foreground(
            session_id=target.descriptor.session_id,
            view_id=event.view_id or "default-view",
            visible=event.visible,
            is_team=target.kind is SessionKind.TEAM,
            has_history=event.has_history,
        )
        self._schedule(action)

    async def execution_started(self, event: SessionExecutionEvent) -> None:
        target = event.target
        action = self._guard().task_started(
            session_id=target.descriptor.session_id,
            is_team=target.kind is SessionKind.TEAM,
            has_history=event.has_history,
        )
        self._schedule(action)

    def execution_finished(self, event: SessionExecutionFinishedEvent) -> None:
        action = self._guard().task_finished(
            session_id=event.target.descriptor.session_id,
            succeeded=event.succeeded,
        )
        self._schedule(action)

    async def session_inactive(self, event: SessionInactiveEvent) -> None:
        target = event.target
        action = self._guard().set_foreground(
            session_id=target.descriptor.session_id,
            view_id="default-view",
            visible=False,
            is_team=target.kind is SessionKind.TEAM,
            has_history=True,
        )
        self._schedule(action)

    @staticmethod
    def _guard() -> kv_cache_task_guard.SessionKVCacheTaskGuard:
        return kv_cache_task_guard.get_session_kv_cache_task_guard()

    def _schedule(
        self,
        action: kv_cache_task_guard.KVCGuardActionRequest | None,
    ) -> None:
        if action is None:
            return

        # Activity hints are latency optimizations. Schedule them independently
        # so chat/execution never waits for remote prefetch or offload I/O.
        task = asyncio.create_task(
            self._dispatch(action),
            name=f"runtime-kvc-{action.action}[{action.session_id}]",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_failure)

    async def _dispatch(
        self,
        action: kv_cache_task_guard.KVCGuardActionRequest,
    ) -> None:
        from openjiuwen.core.session.agent import create_agent_session
        from openjiuwen.core.session.agent_team import create_agent_team_session

        factory = create_agent_team_session if action.is_team else create_agent_session
        session = factory(
            session_id=action.session_id,
            kv_cache_runtime=self._runtime,
        )
        method = (
            session.suspend_kvc if action.action == "offload" else session.prepare_kvc
        )
        await method()

    @staticmethod
    def _log_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            _LOGGER.warning("KVC activity action failed", exc_info=True)

    async def close(self) -> None:
        # Cancelling these JiuwenSwarm-owned hint tasks is local-only. It does
        # not close KVCacheRuntime and therefore cannot send root eviction.
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=KVC_TERMINAL_CLEANUP_TIMEOUT_SECONDS,
            )
            for task in pending:
                task.cancel()


__all__ = ["KVCacheSessionLifecycleParticipant"]
