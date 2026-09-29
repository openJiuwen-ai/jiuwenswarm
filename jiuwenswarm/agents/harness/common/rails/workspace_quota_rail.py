# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""before_tool_call：仅拦 bash / exec_command / mcp_exec_command（§2.3）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail

from jiuwenswarm.common.workspace.quota import (
    WORKSPACE_QUOTA_EXCEEDED,
    QuotaGateDecision,
    WorkspaceQuotaExceeded,
    resolve_effective_quota,
)

logger = logging.getLogger(__name__)

_COMMAND_TOOLS = frozenset(
    {
        "bash",
        "exec_command",
        "mcp_exec_command",
        "execute_bash",
        "run_command",
        "shell",
        "powershell",
    }
)


def check_workspace_command_tool() -> QuotaGateDecision:
    """命令工具无法预估增量；已是 block 则拒绝。"""
    snap = resolve_effective_quota()
    if snap.status == "block":
        decision = QuotaGateDecision(
            allowed=False,
            status=snap.status,
            detail=WORKSPACE_QUOTA_EXCEEDED,
            snapshot=snap,
        )
        raise WorkspaceQuotaExceeded(decision=decision)
    return QuotaGateDecision(allowed=True, status=snap.status, snapshot=snap)


class WorkspaceQuotaRail(AgentRail):
    """命令工具满额时拒绝；写文件工具走 fs.write_file 门禁，此处不查。"""

    priority: int = 95

    def init(self, agent: Any) -> None:
        super().init(agent)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_inputs = ctx.inputs
        if not isinstance(tool_inputs, dict):
            tool_name = getattr(tool_inputs, "tool_name", "") or ""
            tool_args = getattr(tool_inputs, "tool_args", {}) or {}
        else:
            tool_name = tool_inputs.get("tool_name", "") or ""
            tool_args = tool_inputs.get("tool_args", {}) or {}
        if not isinstance(tool_args, dict):
            tool_args = {}

        normalized = str(tool_name or "").strip().lower()
        matched = False
        for cmd in _COMMAND_TOOLS:
            if normalized == cmd or normalized.startswith(cmd + "_"):
                matched = True
                break
        if not matched:
            return

        try:
            # du / walk 可能阻塞数秒；卸到线程池，避免卡住 Agent 事件循环。
            await asyncio.to_thread(check_workspace_command_tool)
        except WorkspaceQuotaExceeded:
            logger.info(
                "[workspace.quota] command tool blocked tool=%s",
                tool_name,
            )
            self._deny(ctx, str(tool_name), tool_args)

    @staticmethod
    def _deny(ctx: AgentCallbackContext, tool_name: str, tool_args: dict[str, Any]) -> None:
        ctx.extra["_skip_tool"] = True
        msg = (
            f"{WORKSPACE_QUOTA_EXCEEDED}: workspace quota exceeded; "
            f"tool '{tool_name}' rejected. Clean up or request expansion."
        )
        if hasattr(ctx.inputs, "tool_result"):
            ctx.inputs.tool_result = {
                "success": False,
                "error": msg,
                "code": WORKSPACE_QUOTA_EXCEEDED,
            }
        elif isinstance(ctx.inputs, dict):
            ctx.inputs["tool_result"] = {
                "success": False,
                "error": msg,
                "code": WORKSPACE_QUOTA_EXCEEDED,
            }
        if hasattr(ctx.inputs, "tool_msg"):
            ctx.inputs.tool_msg = msg


__all__ = ["WorkspaceQuotaRail", "check_workspace_command_tool"]
