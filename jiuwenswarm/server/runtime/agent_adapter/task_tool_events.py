# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Emit subagent roster and activity events for SDK TaskTool synchronous delegations.

The SDK TaskTool (``task_tool``) creates ephemeral subagents — including the
browser_agent — but never registers them with the subagent runtime registry,
and it consumes the subagent stream internally, keeping only the terminal
answer. A synchronous delegation therefore produces no frames on the parent
session stream beyond the tool-call boundaries: Web clients see a silent turn
for the whole task, which for browser work can run to the 600-second
complex-query budget plus one focused resume (issue #4861: browser_agent
shows no event frames for over 10 minutes).

This patch wraps the narrow dispatch seam ``TaskTool._invoke_created_subagent``
to make synchronous delegations observable while leaving every other TaskTool
behavior (validation, browser query budgets, resumes, usage attribution,
cleanup) untouched:

1. roster projections at start, success, and failure;
2. subagent stream chunks (tool calls, tool results, thinking) mirrored to
   the parent session as ``subagent_activity`` frames through the shared
   ``ActivityProjector`` used by the async subagent runtime, renumbered
   with a per-subagent monotonic sequence so resumed delegations never
   collide with the frames the Web client already has;
3. a periodic roster heartbeat so the turn stays observable even while the
   subagent is quiet inside one long tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from typing import Any, AsyncIterator

from .managed_subagent_control import (
    register_cua_execution,
    unregister_cua_execution,
)

_PATCH_APPLIED = False
_REVISIONS: dict[str, int] = {}
# Resumed browser delegations reuse the same sub_session_id, and the Web
# client dedupes activities by ``subagent_id:task_id:seq`` — so the activity
# sequence must keep rising across delegations instead of restarting at 1
# (mirroring the async runtime's per-subagent seq semantics).
_ACTIVITY_SEQS: dict[str, int] = {}
_MAX_TASK_DESCRIPTION_CHARS = 500
_MAX_CONSECUTIVE_EMIT_FAILURES = 3
_HEARTBEAT_INTERVAL_S = 20.0

_logger = logging.getLogger(__name__)


def _next_revision(subagent_id: str) -> int:
    _REVISIONS[subagent_id] = _REVISIONS.get(subagent_id, 0) + 1
    return _REVISIONS[subagent_id]


def _next_activity_seq(subagent_id: str) -> int:
    _ACTIVITY_SEQS[subagent_id] = _ACTIVITY_SEQS.get(subagent_id, 0) + 1
    return _ACTIVITY_SEQS[subagent_id]


def _display_name(subagent: Any, normalized_type: str) -> str:
    card = getattr(subagent, "card", None)
    return str(getattr(card, "name", None) or normalized_type or "subagent")


async def _emit_roster_event(parent_session: Any, projection: dict[str, Any]) -> None:
    try:
        from openjiuwen.harness.subagent_runtime.status_events import (
            emit_subagent_updated,
        )

        await emit_subagent_updated(parent_session, projection=projection)
    except Exception as exc:  # noqa: BLE001 - roster emission must never break the tool
        _logger.warning("[task-tool-events] roster emission failed: %s", exc)


def _build_projection(
    *,
    subagent_id: str,
    normalized_type: str,
    display_name: str,
    parent_session_id: str,
    task_description: str,
    status: Any,
    revision: int,
    created_at_ms: float,
) -> dict[str, Any]:
    from openjiuwen.harness.subagent_runtime.status_events import (
        build_subagent_updated_payload,
    )

    return build_subagent_updated_payload(
        subagent_id=subagent_id,
        subagent_type=normalized_type,
        display_name=display_name,
        role="",
        parent_session_id=parent_session_id,
        task_description=task_description[:_MAX_TASK_DESCRIPTION_CHARS],
        created_at_ms=created_at_ms,
        updated_at_ms=time.time() * 1000,
        closed_at_ms=None,
        status=status,
        revision=revision,
    )


class _SyncActivityEmitter:
    """Failure-isolated ``subagent_activity`` emission for one delegation.

    Stream chunks are mapped to activity milestones with the same
    ``ActivityProjector`` the async subagent runtime uses, so the Web client
    renders synchronous delegations exactly like runtime-managed ones. Every
    emission is guarded: repeated failures disable the emitter instead of
    breaking the delegation (mirroring the runtime's ``ActivityEmitter``).
    """

    def __init__(
        self,
        *,
        parent_session: Any,
        parent_session_id: str,
        subagent_id: str,
    ) -> None:
        self._parent_session = parent_session
        self._parent_session_id = parent_session_id
        self._subagent_id = subagent_id
        self._projector: Any | None = None
        self._consecutive_failures = 0
        self._disabled = False

    def _ensure_projector(self) -> Any:
        if self._projector is None:
            from openjiuwen.harness.subagent_runtime.activity import ActivityProjector
            from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig

            self._projector = ActivityProjector(
                subagent_id=self._subagent_id,
                config=SubagentRuntimeConfig(),
            )
        return self._projector

    async def forward_chunk(self, chunk: Any) -> None:
        """Mirror one subagent stream chunk to the parent session stream."""
        if self._disabled:
            return
        try:
            activities = self._ensure_projector().project(
                chunk,
                task_id=self._subagent_id,
            )
        except Exception as exc:  # noqa: BLE001 - projection must never break the tool
            self._record_failure(exc)
            return
        for activity in activities:
            await self._emit_activity(activity)

    async def flush(self) -> None:
        """Emit the thinking tail still buffered when the subagent stream ends."""
        if self._disabled:
            return
        try:
            pending = self._ensure_projector().flush_pending(task_id=self._subagent_id)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)
            return
        for activity in pending:
            await self._emit_activity(activity)

    async def _emit_activity(self, activity: Any) -> None:
        try:
            from openjiuwen.core.session.stream.base import OutputSchema
            from openjiuwen.harness.subagent_runtime.activity_events import (
                SUBAGENT_ACTIVITY_EVENT_TYPE,
            )

            # The projector restarts its seq per delegation; renumber from the
            # per-subagent registry so resumed delegations never reuse a
            # ``subagent_id:task_id:seq`` the Web client has already seen.
            seq = _next_activity_seq(self._subagent_id)
            if activity.seq != seq:
                activity = dataclasses.replace(activity, seq=seq)
            projection = activity.to_dict()
            projection["parent_session_id"] = self._parent_session_id
            await self._parent_session.write_stream(
                OutputSchema(
                    type=SUBAGENT_ACTIVITY_EVENT_TYPE,
                    index=0,
                    payload={"subagent_activity": projection},
                )
            )
            self._consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)

    def _record_failure(self, exc: Exception) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _MAX_CONSECUTIVE_EMIT_FAILURES:
            self._disabled = True
            _logger.warning(
                "[task-tool-events] sync activity emission disabled after %s failures: %s",
                self._consecutive_failures,
                exc,
            )


class _ActivityForwardingSubagent:
    """Proxy that mirrors a synchronous subagent's stream to the parent session.

    ``TaskTool`` consumes the subagent stream internally and only surfaces the
    terminal answer, so chunks must be mirrored here before they reach it.
    Chunks are yielded through unchanged; ``invoke``-only subagents keep their
    existing behavior because the delegation falls back to ``invoke``.
    """

    def __init__(self, inner: Any, emitter: _SyncActivityEmitter) -> None:
        self._inner = inner
        self._emitter = emitter

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            async for chunk in self._inner.stream(inputs, **kwargs):
                await self._emitter.forward_chunk(chunk)
                yield chunk
        finally:
            await self._emitter.flush()

    async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
        return await self._inner.invoke(inputs, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _run_roster_heartbeat(
    parent_session: Any,
    *,
    parent_session_id: str,
    subagent_id: str,
    normalized_type: str,
    display_name: str,
    task_text: str,
    created_at_ms: float,
) -> None:
    """Touch the roster with a running status while the delegation runs.

    A delegation can sit inside one long tool call without producing any
    stream chunk; the heartbeat keeps ``chat.subtask_update`` frames flowing
    so the Web client can tell a live task from a dead one.
    """
    from openjiuwen.harness.subagent_runtime.models import SubagentStatus

    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        await _emit_roster_event(
            parent_session,
            _build_projection(
                subagent_id=subagent_id,
                normalized_type=normalized_type,
                display_name=display_name,
                parent_session_id=parent_session_id,
                task_description=task_text,
                status=SubagentStatus.running(),
                revision=_next_revision(subagent_id),
                created_at_ms=created_at_ms,
            ),
        )


async def _stop_heartbeat(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _invoke_with_event_forwarding(
    task_tool: Any,
    original_invoke: Any,
    subagent: Any,
    kwargs: dict[str, Any],
) -> Any:
    """Run one synchronous delegation with roster, activity, and heartbeat frames."""
    from openjiuwen.harness.subagent_runtime.models import SubagentStatus

    parent_session = kwargs.get("parent_session")
    sub_session_id = str(kwargs.get("sub_session_id") or "").strip()
    normalized_type = str(kwargs.get("normalized_type") or "").strip()
    parent_session_id = str(kwargs.get("parent_session_id") or "").strip()
    if parent_session is None or not all(
        (sub_session_id, normalized_type, parent_session_id)
    ):
        return await original_invoke(task_tool, subagent, **kwargs)

    display_name_value = _display_name(subagent, normalized_type)
    task_text = str(kwargs.get("task_description") or "")
    started_ms = time.time() * 1000

    await _emit_roster_event(
        parent_session,
        _build_projection(
            subagent_id=sub_session_id,
            normalized_type=normalized_type,
            display_name=display_name_value,
            parent_session_id=parent_session_id,
            task_description=task_text,
            status=SubagentStatus.pending_init(),
            revision=_next_revision(sub_session_id),
            created_at_ms=started_ms,
        ),
    )
    emitter = _SyncActivityEmitter(
        parent_session=parent_session,
        parent_session_id=parent_session_id,
        subagent_id=sub_session_id,
    )
    heartbeat = asyncio.create_task(
        _run_roster_heartbeat(
            parent_session,
            parent_session_id=parent_session_id,
            subagent_id=sub_session_id,
            normalized_type=normalized_type,
            display_name=display_name_value,
            task_text=task_text,
            created_at_ms=started_ms,
        )
    )
    cua_execution = (
        register_cua_execution(
            parent_session_id=parent_session_id,
            sub_session_id=sub_session_id,
        )
        if normalized_type == "cua_agent"
        else None
    )
    if cua_execution is not None:
        _logger.info(
            "[task-tool-events] registered managed CUA execution request=%s "
            "parent_session=%s sub_session=%s",
            cua_execution.request_id,
            parent_session_id,
            sub_session_id,
        )
    try:
        result = await original_invoke(
            task_tool,
            _ActivityForwardingSubagent(subagent, emitter),
            **kwargs,
        )
    except BaseException as exc:
        unregister_cua_execution(cua_execution)
        await _stop_heartbeat(heartbeat)
        await _emit_roster_event(
            parent_session,
            _build_projection(
                subagent_id=sub_session_id,
                normalized_type=normalized_type,
                display_name=display_name_value,
                parent_session_id=parent_session_id,
                task_description=task_text,
                status=SubagentStatus.errored(str(exc) or "subagent failed"),
                revision=_next_revision(sub_session_id),
                created_at_ms=started_ms,
            ),
        )
        raise
    unregister_cua_execution(cua_execution)
    await _stop_heartbeat(heartbeat)
    await _emit_roster_event(
        parent_session,
        _build_projection(
            subagent_id=sub_session_id,
            normalized_type=normalized_type,
            display_name=display_name_value,
            parent_session_id=parent_session_id,
            task_description=task_text,
            status=SubagentStatus.completed(),
            revision=_next_revision(sub_session_id),
            created_at_ms=started_ms,
        ),
    )
    return result


def apply_task_tool_event_patch() -> None:
    """Patch TaskTool so synchronous delegations emit observable events."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    from openjiuwen.harness.tools.subagent.task_tool import TaskTool

    if getattr(TaskTool, "roster_event_patch_applied", False):
        _PATCH_APPLIED = True
        return

    original_invoke = getattr(TaskTool, "_invoke_created_subagent", None)
    if not callable(original_invoke):
        _logger.warning(
            "TaskTool roster events are unavailable because this SDK does not "
            "expose the _invoke_created_subagent seam; TaskTool behavior is unchanged."
        )
        TaskTool.roster_event_patch_applied = True
        TaskTool.roster_event_patch_mode = "unsupported_sdk"
        _PATCH_APPLIED = True
        return

    async def _invoke_created_subagent_with_events(self: Any, subagent: Any, **kwargs: Any) -> Any:
        return await _invoke_with_event_forwarding(self, original_invoke, subagent, kwargs)

    setattr(TaskTool, "_invoke_created_subagent", _invoke_created_subagent_with_events)
    TaskTool.roster_event_patch_applied = True
    TaskTool.roster_event_patch_mode = "dispatch_only"
    _PATCH_APPLIED = True


__all__ = ["apply_task_tool_event_patch"]
