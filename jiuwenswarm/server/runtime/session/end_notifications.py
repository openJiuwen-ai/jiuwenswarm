# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge a quiesced Runtime session run to the Worker-owned EventBus."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.extensions_v2.event_types.session_end import (
    SESSION_END_EVENT,
    EndInvocation,
    SessionEndInput,
    SessionResume,
    bind_end_invocation,
)
from jiuwenswarm.runtime.session_lifecycle import SessionKind, SessionRunEndEvent, SessionRunEndReason

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus
    from jiuwenswarm.extensions_v2.management import ExtensionManager

logger = logging.getLogger(__name__)

SESSION_END_TIMEOUT_SECONDS = 1.5


class ExtensionSessionEndParticipant:
    """Give plugins bounded cleanup time when one main-session run ends.

    Runtime owns quiescence and release. Ending a run does not require deleting
    its history, and cleanup cannot veto the original shutdown.
    """

    def __init__(
        self, *, timeout_seconds: float = SESSION_END_TIMEOUT_SECONDS,
        release_attachments: Callable[[SessionRunEndEvent], Awaitable[None]] | None = None,
    ) -> None:
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("session.end timeout must be finite and positive")
        self._timeout = timeout_seconds
        self._attempted: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._release_attachments = release_attachments

    @property
    def name(self) -> str:
        return "extensions_v2.session.end"

    async def session_run_ended(self, event: SessionRunEndEvent) -> None:
        """Await bounded user cleanup before the original platform release."""
        from jiuwenswarm.extensions_v2.assembly import get_extension_bus, get_extension_manager

        target = event.target
        if target.kind is not SessionKind.AGENT:
            return
        descriptor = target.descriptor
        session_id = descriptor.session_id
        if not is_valid_session_id(session_id):
            raise ValueError("session.end requires a valid session_id")
        if type(event.generation) is not int or event.generation < 1:
            raise ValueError("session.end requires a real Runtime generation")
        reason = SessionRunEndReason(event.reason)
        if self._attempted.get(session_id, 0) >= event.generation:
            return
        bus, manager = get_extension_bus(), get_extension_manager()
        if bus is None or manager is None or not manager.initialized:
            if self._release_attachments is not None:
                await self._release_attachments(event)
            return
        value = SessionEndInput(
            reason=reason.value, sessionId=session_id, resume=SessionResume(session_id),
        )
        root = descriptor.project_dir
        project_root = root if root and Path(root).is_absolute() else None
        context = SessionContext(session_id, metadata={"project_root": project_root})
        state = EndInvocation(value)
        self._attempted[session_id] = event.generation
        task = asyncio.create_task(self._dispatch(bus, manager, state, context, event), name="extension-session-end")
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
            logger.warning("[ExtensionsV2] session.end timed out; Runtime release continues: session_id=%s", session_id)
            return
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("[ExtensionsV2] session.end handler cancelled; Runtime release continues: session_id=%s", session_id)
        except Exception:
            logger.warning("[ExtensionsV2] session.end failed; Runtime release continues: session_id=%s", session_id, exc_info=True)

    async def _dispatch(
        self,
        bus: ExtensionEventBus,
        manager: ExtensionManager,
        state: EndInvocation,
        context: SessionContext,
        event: SessionRunEndEvent,
    ) -> None:
        runtime = manager.registry.runtime
        packages = tuple(record.package_id for record in runtime.active_packages())
        runtime.begin_calls(packages)
        try:
            with bind_end_invocation(state):
                if self._release_attachments is not None:
                    await self._release_attachments(event)
                result = await bus.trigger(Event(SESSION_END_EVENT, state.original), context)
                if state.receipt is None or result != state.receipt:
                    raise RuntimeError("session.end did not return the actual CORE receipt")
        finally:
            runtime.end_calls(packages)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()
