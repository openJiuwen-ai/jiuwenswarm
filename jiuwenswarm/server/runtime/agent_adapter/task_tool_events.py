# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Emit subagent roster events for SDK TaskTool synchronous delegations.

The SDK TaskTool (``task_tool``) creates ephemeral subagents — including the
browser_agent — but never registers them with the subagent runtime registry,
so the parent session stream never carries ``subagent_updated`` projections
and Web clients cannot learn that a browser agent exists (the browser tab
never appears). This patch wraps the narrow dispatch seam
``TaskTool._invoke_created_subagent`` to emit roster projections at start,
success, and failure while leaving every other TaskTool behavior (validation,
browser query budgets, resumes, usage attribution, cleanup) untouched.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_PATCH_APPLIED = False
_REVISIONS: dict[str, int] = {}
_MAX_TASK_DESCRIPTION_CHARS = 500

_logger = logging.getLogger(__name__)


def _next_revision(subagent_id: str) -> int:
    _REVISIONS[subagent_id] = _REVISIONS.get(subagent_id, 0) + 1
    return _REVISIONS[subagent_id]


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
    sub_session_id: str,
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
        subagent_id=sub_session_id,
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


def apply_task_tool_event_patch() -> None:
    """Patch TaskTool so synchronous delegations emit roster events."""
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
        from openjiuwen.harness.subagent_runtime.models import SubagentStatus

        parent_session = kwargs.get("parent_session")
        sub_session_id = str(kwargs.get("sub_session_id") or "").strip()
        normalized_type = str(kwargs.get("normalized_type") or "").strip()
        parent_session_id = str(kwargs.get("parent_session_id") or "").strip()
        if parent_session is None or not all(
            (sub_session_id, normalized_type, parent_session_id)
        ):
            return await original_invoke(self, subagent, **kwargs)

        display_name_value = _display_name(subagent, normalized_type)
        task_text = str(kwargs.get("task_description") or "")
        started_ms = time.time() * 1000

        await _emit_roster_event(
            parent_session,
            _build_projection(
                sub_session_id=sub_session_id,
                normalized_type=normalized_type,
                display_name=display_name_value,
                parent_session_id=parent_session_id,
                task_description=task_text,
                status=SubagentStatus.pending_init(),
                revision=_next_revision(sub_session_id),
                created_at_ms=started_ms,
            ),
        )
        try:
            result = await original_invoke(self, subagent, **kwargs)
        except BaseException as exc:
            await _emit_roster_event(
                parent_session,
                _build_projection(
                    sub_session_id=sub_session_id,
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
        await _emit_roster_event(
            parent_session,
            _build_projection(
                sub_session_id=sub_session_id,
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

    setattr(TaskTool, "_invoke_created_subagent", _invoke_created_subagent_with_events)
    TaskTool.roster_event_patch_applied = True
    TaskTool.roster_event_patch_mode = "dispatch_only"
    _PATCH_APPLIED = True


__all__ = ["apply_task_tool_event_patch"]
