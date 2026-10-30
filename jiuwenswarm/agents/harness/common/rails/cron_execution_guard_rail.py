# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution-time guard for cron job-store mutations inside a cron run.

Cron execution sessions already have their cron management tools removed
(``CronRuntimeBridge.build_tools`` / ``_ensure_cron_tools_registered``), which
covers the tool registration surface.  The remaining bypass is the generic
file/shell/code surface: a cron run can still write ``cron_jobs.json`` or call
``FileCronJobStore.create_job`` from a script, deriving a nested job.

This rail rejects those calls when the current session is a cron execution
session.  Ordinary sessions are untouched, and cron runs keep full file/shell
access for the work the scheduled job actually performs (read_file, writing
project files, running commands).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
)
from jiuwenswarm.common.cron_session import is_cron_execution_session
from jiuwenswarm.common.utils import logger

# Tools that can execute arbitrary code and therefore reach the store or the
# package API behind the removed cron tools.
_SHELL_TOOLS = frozenset(
    {
        "bash",
        "sh",
        "shell",
        "powershell",
        "cmd",
        "execute_cmd",
        "run_command",
        "mcp_exec_command",
        "create_terminal",
        "code",
        "execute_code",
        "python",
    }
)

# Tools that persist caller-provided content to a file.
_FILE_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "write_text_file",
        "write",
        "create_file",
        "create",
        "edit_file",
        "edit",
        "search_replace",
        "str_replace",
        "apply_patch",
        "multi_edit",
        "append_file",
    }
)

# Distinctive names that identify the cron job store or its write API.  A cron
# run has no legitimate reason to reference any of them: management goes
# through the cron tools, which are not registered in a cron execution session.
_CRON_STORE_MARKERS = (
    "cron_jobs.json",
    "cron_jobs",
    # Store / mutation helpers in the runtime package.
    "jiuwenswarm.runtime.cron",
    "jiuwenswarm/runtime/cron",
    "jiuwenswarm\\runtime\\cron",
    "filecronjobstore",
    "cronjobstore",
    "get_cron_jobs_path",
    "build_new_cron_job",
    "apply_cron_job_patch",
    "cron_job_mutations",
    # Runtime tool bridge and gateway controller (the other write entry points).
    "jiuwenswarm.agents.harness.common.tools.cron",
    "jiuwenswarm/agents/harness/common/tools/cron",
    "cronruntimebridge",
    "jiuwenswarm.gateway.cron",
    "jiuwenswarm/gateway/cron",
    "croncontroller",
)

# Gateway mutation commands; reaching them from a shell would also derive jobs
# while bypassing the removed cron tools.
_CRON_MUTATION_COMMANDS = (
    "cron.job.create",
    "cron.job.update",
    "cron.job.delete",
    "cron.job.toggle",
    "cron.job.run_now",
)

_MARKERS = tuple(marker.casefold() for marker in (*_CRON_STORE_MARKERS, *_CRON_MUTATION_COMMANDS))

_DENIAL_MESSAGE = (
    "[CRON_NESTED_BLOCKED] 定时任务执行会话内不允许创建或修改定时任务，"
    "已阻止对定时任务存储的访问。请告知用户：cron 会话内不允许创建新的定时任务，"
    "请在普通对话会话中操作。"
)


def _session_id(ctx: AgentCallbackContext) -> str:
    """Return the executing session id without assuming a concrete Session type."""
    session = getattr(ctx, "session", None)
    getter = getattr(session, "get_session_id", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:  # noqa: BLE001 - identity lookup must never break a tool call
            return ""
    return str(getattr(session, "_session_id", "") or "").strip()


def _in_cron_execution_session(ctx: AgentCallbackContext) -> bool:
    """Whether the current tool call belongs to a scheduled-job execution run."""
    if is_cron_execution_session(_session_id(ctx)):
        return True
    return str(TOOL_PERMISSION_CHANNEL_ID.get() or "").strip() in {"cron", "__cron__"}


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_call_data(ctx: AgentCallbackContext) -> tuple[str, dict[str, Any]]:
    inputs = ctx.inputs
    if isinstance(inputs, dict):
        tool_name = inputs.get("tool_name", "")
        raw_args = inputs.get("tool_args", {})
    else:
        tool_name = getattr(inputs, "tool_name", "")
        raw_args = getattr(inputs, "tool_args", {})
    return str(tool_name or "").lower(), _parse_tool_args(raw_args)


def _iter_strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string nested in *value* (bounded so a pathological arg tree
    cannot stall the hook)."""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_strings(item, depth + 1)


def _references_cron_store(tool_name: str, tool_args: dict[str, Any]) -> bool:
    """Whether a write-capable tool call targets the cron job store."""
    if tool_name not in _SHELL_TOOLS and tool_name not in _FILE_WRITE_TOOLS:
        return False
    for text in _iter_strings(tool_args):
        lowered = text.casefold()
        if any(marker in lowered for marker in _MARKERS):
            return True
    return False


class CronExecutionGuardRail(DeepAgentRail):
    """Reject cron-store mutations attempted through generic tools during a cron run."""

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not _in_cron_execution_session(ctx):
            return
        tool_name, tool_args = _tool_call_data(ctx)
        if not _references_cron_store(tool_name, tool_args):
            return
        logger.warning(
            "[CronExecutionGuard] blocked cron store access in cron execution session: tool=%s",
            tool_name,
        )
        self._reject_tool(ctx)

    @staticmethod
    def _reject_tool(ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        tool_call = (
            inputs.get("tool_call")
            if isinstance(inputs, dict)
            else getattr(inputs, "tool_call", None)
        )
        tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
        ctx.extra["_skip_tool"] = True
        if isinstance(inputs, dict):
            inputs["tool_result"] = _DENIAL_MESSAGE
            inputs["tool_msg"] = ToolMessage(
                content=_DENIAL_MESSAGE, tool_call_id=tool_call_id
            )
        else:
            inputs.tool_result = _DENIAL_MESSAGE
            inputs.tool_msg = ToolMessage(
                content=_DENIAL_MESSAGE, tool_call_id=tool_call_id
            )


__all__ = ["CronExecutionGuardRail"]
