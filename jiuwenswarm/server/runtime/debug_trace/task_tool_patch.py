# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Attach subagent stream capture without replacing SDK TaskTool semantics.

The SDK owns input validation, query budgets, focused browser resumes, resource
cleanup, usage attribution, and authoritative result rendering. This patch only
replaces its narrow subagent dispatch seam while a debug dump is active.
"""

from __future__ import annotations

import logging
from typing import Any


_PATCH_APPLIED = False
_logger = logging.getLogger(__name__)


def _subagent_source_label(subagent: Any) -> str:
    card = getattr(subagent, "card", None)
    subagent_type = str(
        getattr(card, "name", None) or getattr(card, "id", None) or "unknown"
    )
    return f"subagent:builtin:{subagent_type}"


def apply_task_tool_debug_patch() -> None:
    """Patch only the SDK dispatch seam used to invoke a created subagent."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    from openjiuwen.harness.tools.subagent.task_tool import TaskTool

    if getattr(TaskTool, "debug_trace_patch_applied", False):
        _PATCH_APPLIED = True
        return

    original_dispatch = getattr(TaskTool, "_invoke_subagent", None)
    if not callable(original_dispatch):
        _logger.warning(
            "TaskTool debug subagent-flow capture is unavailable because this SDK "
            "does not expose the narrow dispatch hook; TaskTool behavior is unchanged."
        )
        TaskTool.debug_trace_patch_applied = True
        TaskTool.debug_trace_patch_mode = "unsupported_sdk"
        _PATCH_APPLIED = True
        return

    async def _invoke_subagent_with_trace(
        self: Any,
        subagent: Any,
        subagent_inputs: dict[str, Any],
        *,
        parent_session_id: str,
        session: Any = None,
    ) -> Any:
        from jiuwenswarm.server.runtime.debug_trace import (
            get_debug_trace_logger,
            invoke_subagent_with_trace,
        )
        from jiuwenswarm.server.runtime.debug_trace.context import (
            get_debug_trace_logger_for_session,
        )

        debug_logger = get_debug_trace_logger()
        if debug_logger is None:
            debug_logger = get_debug_trace_logger_for_session(parent_session_id)
        if debug_logger is None or not debug_logger.captures_subagent_flow():
            return await original_dispatch(
                self,
                subagent,
                subagent_inputs,
                parent_session_id=parent_session_id,
                session=session,
            )
        return await invoke_subagent_with_trace(
            subagent,
            inputs=subagent_inputs,
            session=session,
            session_id=parent_session_id,
            source_label=_subagent_source_label(subagent),
        )

    TaskTool._invoke_subagent = _invoke_subagent_with_trace
    TaskTool.debug_trace_patch_applied = True
    TaskTool.debug_trace_patch_mode = "dispatch_only"
    _PATCH_APPLIED = True


__all__ = ["apply_task_tool_debug_patch"]
