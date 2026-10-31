# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge Runtime's terminal delete phase to the Worker-owned EventBus."""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
import uuid
from typing import TYPE_CHECKING

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.extensions_v2.event_types.session_end import (
    SESSION_END_EVENT,
    EndInvocation,
    SessionEndInput,
    bind_end_invocation,
)
from jiuwenswarm.runtime.session_lifecycle import SessionKind, SessionLifecycleTarget

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus
    from jiuwenswarm.extensions_v2.management import ExtensionManager

logger = logging.getLogger(__name__)

SESSION_END_TIMEOUT_SECONDS = 2.0


class ExtensionSessionEndParticipant:
    """Notify once per unfinished main-Session deletion in this Runtime.

    The existing deletion transaction owns quiescence, resource release and
    persistence. User cleanup is best-effort and cannot veto that transaction.
    """

    def __init__(self, *, timeout_seconds: float = SESSION_END_TIMEOUT_SECONDS) -> None:
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("session.end timeout must be finite and positive")
        self._timeout = timeout_seconds
        self._attempted: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def name(self) -> str:
        return "extensions_v2.session.end"

    async def before_delete(self, target: SessionLifecycleTarget) -> None:
        """Defer notification until Runtime has quiesced target execution."""
        del target

    async def release_resources(self, target: SessionLifecycleTarget) -> None:
        """Await bounded user cleanup before the original platform release."""
        from jiuwenswarm.extensions_v2.assembly import get_extension_bus, get_extension_manager

        if target.kind is not SessionKind.AGENT:
            return
        descriptor = target.descriptor
        session_id = descriptor.session_id
        if not is_valid_session_id(session_id):
            raise ValueError("session.end requires a valid session_id")
        if session_id in self._attempted:
            return
        bus, manager = get_extension_bus(), get_extension_manager()
        if bus is None or manager is None or not manager.initialized:
            return
        value = SessionEndInput(
            session_id=session_id, end_id=uuid.uuid4().hex, reason="session_deleted",
            channel_id=descriptor.channel_id, mode=descriptor.mode,
        )
        root = descriptor.project_dir
        project_root = root if root and Path(root).is_absolute() else None
        context = SessionContext(session_id, metadata={"project_root": project_root})
        state = EndInvocation(value)
        self._attempted.add(session_id)
        task = asyncio.create_task(self._dispatch(bus, manager, state, context), name="extension-session-end")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        try:
            done, _pending = await asyncio.wait({task}, timeout=self._timeout)
        except BaseException:
            state.active = False
            task.cancel()
            raise
        if not done:
            state.active = False
            task.cancel()
            logger.warning("[ExtensionsV2] session.end timed out; deletion continues: session_id=%s", session_id)
            return
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("[ExtensionsV2] session.end handler cancelled; deletion continues: session_id=%s", session_id)
        except Exception:
            logger.warning("[ExtensionsV2] session.end failed; deletion continues: session_id=%s", session_id, exc_info=True)

    async def delete_failed(self, target: SessionLifecycleTarget, *, destructive_started: bool) -> None:
        """Do not repeat externally visible cleanup after a failed delete."""
        del target, destructive_started

    async def delete_committed(self, target: SessionLifecycleTarget) -> None:
        """Forget completed state; Runtime already deduplicates committed deletes."""
        self._attempted.discard(target.descriptor.session_id)

    @staticmethod
    async def _dispatch(
        bus: ExtensionEventBus,
        manager: ExtensionManager,
        state: EndInvocation,
        context: SessionContext,
    ) -> None:
        runtime = manager.registry.runtime
        packages = tuple(record.package_id for record in runtime.active_packages())
        runtime.begin_calls(packages)
        try:
            with bind_end_invocation(state):
                result = await bus.trigger(Event(SESSION_END_EVENT, state.original), context)
                if state.receipt is None or result != state.receipt:
                    raise RuntimeError("session.end did not return the actual CORE receipt")
        finally:
            runtime.end_calls(packages)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()
