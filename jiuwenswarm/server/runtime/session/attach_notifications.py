# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observe committed foreground transitions and notify the Worker EventBus."""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING
import uuid

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.extensions_v2.event_types.session_attach import (
    SESSION_ATTACH_EVENT,
    AttachInvocation,
    SessionAttachInput,
    bind_attach_invocation,
)
from jiuwenswarm.runtime.session_lifecycle import SessionForegroundEvent, SessionKind

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus
    from jiuwenswarm.extensions_v2.management import ExtensionManager

logger = logging.getLogger(__name__)
_SURFACE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
SESSION_ATTACH_TIMEOUT_SECONDS = 2.0


class SessionAssociationStore:
    """Runtime-local view bindings; event ids are issued only on real changes."""

    def __init__(self) -> None:
        self._views: dict[tuple[str, str], SessionAttachInput] = {}
        self._contexts: dict[tuple[str, str], SessionContext] = {}

    def get(self, channel_id: str, surface_id: str) -> SessionAttachInput | None:
        """Return immutable current facts for a specific view."""
        return self._views.get((channel_id, surface_id))

    def commit(self, event: SessionForegroundEvent, context: SessionContext | None = None) -> SessionAttachInput | None:
        """Commit a confirmed main-Session view change, ignoring exact replays."""
        target = event.target.descriptor
        key = (target.channel_id, event.view_id)
        previous = self._views.get(key)
        if previous is not None and previous.session_id == target.session_id:
            return None
        value = SessionAttachInput(
            session_id=target.session_id, surface_id=event.view_id, attachment_id=uuid.uuid4().hex,
            channel_id=target.channel_id, mode=target.mode,
            previous_session_id=previous.session_id if previous is not None else None,
        )
        self._views[key] = value
        self._contexts[key] = context if context is not None else SessionContext(target.session_id)
        return value

    def context(self, value: SessionAttachInput) -> SessionContext:
        """Read the original context before replacing or releasing its binding."""
        if not self.is_current(value):
            raise ValueError("association is no longer current")
        return self._contexts[(value.channel_id, value.surface_id)]

    def is_current(self, value: SessionAttachInput) -> bool:
        """A replaced association cannot later be acknowledged as current."""
        return self.get(value.channel_id, value.surface_id) is value

    def forget_view(self, channel_id: str, surface_id: str) -> None:
        """Discard the observed main association when the view leaves scope."""
        self._views.pop((channel_id, surface_id), None)
        self._contexts.pop((channel_id, surface_id), None)


def _confirmed_context(event: SessionForegroundEvent) -> SessionContext | None:
    from jiuwenswarm.common.mode_matrix import is_team_mode
    from jiuwenswarm.server.runtime.session import lifecycle
    from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    sid = event.target.descriptor.session_id
    try:
        lifecycle.guard(sid)
        path, error = resolve_session_dir(sid, create=False)
        if error or path is None or not path.is_dir():
            return None
        saved = get_session_metadata(sid, enable_writeback=False)
        if not saved or is_team_mode(saved.get("mode")):
            return None
    except (lifecycle.LifecycleError, OSError, ValueError):
        return None
    root = saved.get("project_dir")
    root = root if isinstance(root, str) and root and Path(root).is_absolute() else None
    return SessionContext(sid, metadata={"project_root": root})


class ExtensionSessionAttachParticipant:
    """Translate committed view transitions into bounded USER notifications."""

    def __init__(self, *, timeout_seconds: float = SESSION_ATTACH_TIMEOUT_SECONDS) -> None:
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("session.attach timeout must be finite and positive")
        self.associations = SessionAssociationStore()
        self._timeout = timeout_seconds
        self._serial = 0
        self._latest: dict[tuple[str, str], int] = {}
        self._tasks: set[asyncio.Task] = set()

    async def foreground_changed(self, event: SessionForegroundEvent) -> None:
        """Commit valid view facts before notifying, preserving replay semantics."""
        from jiuwenswarm.extensions_v2.assembly import get_extension_bus, get_extension_manager

        if not event.visible:
            return
        descriptor = event.target.descriptor
        if not isinstance(event.view_id, str) or _SURFACE_ID.fullmatch(event.view_id) is None:
            raise ValueError("session.attach requires a valid view_id")
        if not is_valid_session_id(descriptor.session_id):
            raise ValueError("session.attach requires a valid session_id")
        key = (descriptor.channel_id, event.view_id)
        self._serial += 1
        serial = self._serial
        self._latest[key] = serial
        previous = self.associations.get(*key)
        previous_context = self.associations.context(previous) if previous is not None else None
        if event.target.kind is not SessionKind.AGENT:
            self.associations.forget_view(*key)
            if previous is not None:
                await self._notify_detach(previous, previous_context, reason="switch", next_session_id=None)
            return
        context = await asyncio.to_thread(_confirmed_context, event)
        if context is None or self._latest.get(key) != serial:
            return
        # Capture after the await: a newer explicit release can invalidate this
        # transition, and another view must not supply its former identity.
        previous = self.associations.get(*key)
        previous_context = self.associations.context(previous) if previous is not None else None
        value = self.associations.commit(event, context)
        if value is None:
            return
        if previous is not None:
            await self._notify_detach(previous, previous_context, reason="switch", next_session_id=value.session_id)
        if not self.associations.is_current(value):
            return
        bus, manager = get_extension_bus(), get_extension_manager()
        if bus is None or manager is None or not manager.initialized:
            return
        async def validate_target() -> bool:
            return await asyncio.to_thread(_confirmed_context, event) is not None

        state = AttachInvocation(value, lambda: self.associations.is_current(value), validate_target)
        task = asyncio.create_task(self._dispatch(bus, manager, state, context), name="extension-session-attach")
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
            logger.warning("[ExtensionsV2] session.attach timed out; association remains: session_id=%s", value.session_id)
            return
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("[ExtensionsV2] session.attach cancelled; association remains: session_id=%s", value.session_id)
        except Exception:
            logger.warning("[ExtensionsV2] session.attach notification failed: session_id=%s", value.session_id, exc_info=True)

    async def detach(self, *, channel_id: str, session_id: str, view_id: str, attachment_id: str) -> bool:
        """Remove only an explicitly identified current view association."""
        if (
            not isinstance(channel_id, str) or not channel_id
            or not isinstance(view_id, str) or _SURFACE_ID.fullmatch(view_id) is None
            or not isinstance(session_id, str) or not is_valid_session_id(session_id)
            or not isinstance(attachment_id, str) or re.fullmatch(r"[0-9a-f]{32}", attachment_id) is None
        ):
            raise ValueError("session.detach requires valid session_id, view_id and attachment_id")
        value = self.associations.get(channel_id, view_id)
        if value is None:
            return False
        if value.session_id != session_id or value.attachment_id != attachment_id:
            raise LookupError("session.detach identity does not match the current attachment")
        context = self.associations.context(value)
        self._serial += 1
        self._latest[(channel_id, view_id)] = self._serial
        self.associations.forget_view(channel_id, view_id)
        await self._notify_detach(value, context, reason="detach", next_session_id=None)
        return True

    async def _notify_detach(
        self,
        value: SessionAttachInput,
        context: SessionContext,
        *,
        reason: str,
        next_session_id: str | None,
    ) -> None:
        from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachInput
        from jiuwenswarm.server.runtime.session.detach_notifications import notify_session_detach

        facts = SessionDetachInput(
            value.session_id, value.surface_id, value.attachment_id, value.channel_id,
            value.mode, reason, next_session_id,
        )
        await notify_session_detach(
            facts, context, lambda: not self.associations.is_current(value),
            timeout_seconds=self._timeout, tasks=self._tasks, task_done=self._task_done,
        )

    @staticmethod
    async def _dispatch(
        bus: ExtensionEventBus,
        manager: ExtensionManager,
        state: AttachInvocation,
        context: SessionContext,
    ) -> None:
        runtime = manager.registry.runtime
        packages = tuple(record.package_id for record in runtime.active_packages())
        runtime.begin_calls(packages)
        try:
            with bind_attach_invocation(state):
                result = await bus.trigger(Event(SESSION_ATTACH_EVENT, state.original), context)
                if (
                    not state.is_current() or state.receipt is None or result != state.receipt
                    or not await state.validate_target() or not state.is_current()
                ):
                    raise RuntimeError("session.attach requires the actual current CORE receipt")
        finally:
            runtime.end_calls(packages)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()
