# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monkeypatch the SDK's builtin ``TaskTool.invoke`` to capture subagent streams.

The builtin TaskTool (``openjiuwen.harness.tools.subagent.task_tool``) drives
subagents via ``await subagent.invoke(...)``, which returns only the final
output string — its internal reasoning / tool calls / token usage never reach
the parent run's :class:`DebugTraceLogger`. This patch wraps ``invoke`` so that,
when a debug run is actively capturing subagent flow, the subagent is instead
driven through ``stream()`` via :func:`invoke_subagent_with_trace` and its
chunks land in the dump under ``source=subagent:builtin:<type>``.

Any run that is NOT capturing (no debug run, or ``include_subagent_flow`` off)
short-circuits to the pristine original ``invoke`` (``_orig_invoke``) for zero
behaviour change — the re-implemented parse/create/invoke path runs only while
a dump is actually being written. Safe to apply unconditionally at startup.
"""

from __future__ import annotations

import uuid

_PATCH_APPLIED = False


def _build_sub_session_id(parent_session_id: str, subagent_type: str) -> str:
    """Mirror of ``TaskTool._build_sub_session_id``.

    Inlined here (rather than calling the SDK's protected staticmethod) so the
    patch stays self-contained. ``browser_agent`` / ``verification_agent`` get a
    deterministic id so their sessions can be resumed on a retry loop, matching
    the SDK convention.
    """
    normalized = str(subagent_type or "").strip()
    if normalized in ("browser_agent", "verification_agent"):
        return f"{parent_session_id}_sub_{normalized}"
    return f"{parent_session_id}_sub_{normalized}_{uuid.uuid4().hex[:8]}"


def apply_task_tool_debug_patch() -> None:
    """Patch ``TaskTool.invoke`` to capture subagent streams under ``/debug``.

    Idempotent: a module flag plus a class-attribute guard make repeat calls
    no-ops.
    """
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    from openjiuwen.harness.tools.subagent.task_tool import TaskTool

    if getattr(TaskTool, "debug_trace_patch_applied", False):
        _PATCH_APPLIED = True
        return

    _orig_invoke = TaskTool.invoke

    async def _invoke_with_trace(self, inputs, **kwargs):  # type: ignore[no-untyped-def]
        from openjiuwen.core.common.exception.codes import StatusCode
        from openjiuwen.core.common.exception.errors import build_error
        from openjiuwen.core.common.logging import logger
        from openjiuwen.core.session.agent import Session
        from openjiuwen.harness.tools.base_tool import ToolOutput

        from jiuwenswarm.server.runtime.debug_trace import (
            get_debug_trace_logger,
            invoke_subagent_with_trace,
        )
        from jiuwenswarm.server.runtime.debug_trace.context import (
            get_debug_trace_logger_for_session,
        )
        from jiuwenswarm.server.runtime.usage_cost import (
            CostLimitExceededError,
            get_subagent_usage_sink,
        )

        # Not capturing and no parent usage sink -> pristine original SDK path, unchanged.
        dbg = get_debug_trace_logger()
        if dbg is None:
            # TaskTool.invoke runs in the DeepAgent's supervisor task (created at
            # session setup, before any /debug request publishes the ContextVar),
            # so the per-request binding above doesn't reach it. Fall back to the
            # session-keyed registry — we hold the parent Session in kwargs.
            parent_session = kwargs.get("session", None)
            if isinstance(parent_session, Session):
                dbg = get_debug_trace_logger_for_session(parent_session.get_session_id())
                if dbg is not None:
                    logger.info(
                        "[TaskTool] (debug) recovered trace logger via session "
                        "registry: %s", parent_session.get_session_id(),
                    )
        if (dbg is None or not dbg.captures_subagent_flow()) and get_subagent_usage_sink() is None:
            return await _orig_invoke(self, inputs, **kwargs)

        parent_session = kwargs.get("session", None)
        if not isinstance(parent_session, Session):
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason="TaskTool requires a valid session in kwargs",
            )

        if isinstance(inputs, dict):
            subagent_type = inputs.get("subagent_type")
            task_description = inputs.get("task_description")
        else:
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason=f"Invalid inputs type: {type(inputs)}",
            )

        if not subagent_type or not task_description:
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason="Both 'subagent_type' and 'task' are required",
            )

        parent_session_id = parent_session.get_session_id()
        sub_session_id = _build_sub_session_id(parent_session_id, str(subagent_type))
        logger.info(
            "[TaskTool] (debug) Creating subagent: %s, parent_session=%s, sub_session=%s",
            subagent_type, parent_session_id, sub_session_id,
        )

        try:
            subagent = self.parent_agent.create_subagent(subagent_type, sub_session_id)
        except Exception as exc:
            logger.error(
                "[TaskTool] Subagent creation failed: type=%s, error=%s",
                subagent_type, exc,
            )
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason=f"Subagent {subagent_type} creation failed: {exc}",
            ) from exc

        try:
            result = await invoke_subagent_with_trace(
                subagent,
                inputs={"query": task_description, "conversation_id": sub_session_id},
                session=parent_session,
                source_label=f"subagent:builtin:{subagent_type}",
            )
            output = result.get("output", "")
            return ToolOutput(
                success=True,
                data={"output": output, "agent_id": subagent.card.id},
                error=None,
            )
        except CostLimitExceededError:
            raise
        except Exception as exc:
            logger.error(
                "[TaskTool] Subagent: %s execution failed, error=%s",
                subagent_type, exc,
            )
            raise build_error(
                StatusCode.TOOL_TASK_TOOL_INVOKED,
                reason=f"Subagent {subagent_type} execution failed: {exc}",
            ) from exc

    TaskTool.invoke = _invoke_with_trace  # type: ignore[assignment]
    TaskTool.debug_trace_patch_applied = True
    _PATCH_APPLIED = True


__all__ = ["apply_task_tool_debug_patch"]
