# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CodeConfirmInterruptRail — user-visible confirmation for sensitive control tools."""

from __future__ import annotations

import json
from typing import Any, Optional

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmInterruptRail

_SWITCH_MODE_EXIT_PLAN_MSG_CN = (
    "[AgentModeRail] plan 模式下不能用 switch_mode 退出。"
    "请先调用 exit_plan_mode 提交计划，再在对话中回复「按计划实现」等批准执行；"
    "或使用 /mode code.normal 切换模式。"
)

_SWITCH_MODE_EXIT_PLAN_MSG_EN = (
    "[AgentModeRail] switch_mode cannot exit plan mode. "
    "Call exit_plan_mode, then approve in chat (e.g. implement the plan), "
    "or use /mode code.normal."
)


def _format_args_preview(tool_args: dict[str, Any]) -> str:
    if not tool_args:
        return ""
    try:
        return json.dumps(tool_args, ensure_ascii=False, indent=2)[:800]
    except (TypeError, ValueError):
        return str(tool_args)[:800]


def build_confirm_interrupt_message(tool_name: str, tool_args: dict[str, Any] | None = None) -> str:
    """Build a descriptive confirmation prompt for the frontend."""
    args = tool_args or {}
    if tool_name == "switch_mode":
        target_mode = str(args.get("mode") or args.get("target_mode") or "").strip()
        if target_mode == "plan":
            action = "enter plan mode (read-only planning phase)"
        elif target_mode in {"normal", "auto"}:
            action = "exit plan mode and return to execution"
        else:
            action = f"switch agent mode to `{target_mode or 'unknown'}`"
        lines = [
            f"**Agent wants to {action}**",
            "",
            "Tool: `switch_mode`",
        ]
        if target_mode:
            lines.append(f"Target mode: `{target_mode}`")
        lines.append("")
        lines.append("Approve to let the agent continue with this mode change.")
        return "\n".join(lines)

    lines = [
        f"**Tool `{tool_name}` requires your approval**",
        "",
        "The agent is waiting for you to allow or reject this action.",
    ]
    preview = _format_args_preview(args)
    if preview and preview != "{}":
        lines.extend(["", "Arguments:", f"```json\n{preview}\n```"])
    return "\n".join(lines)


class CodeConfirmInterruptRail(ConfirmInterruptRail):
    """ConfirmInterruptRail with tool-specific confirmation copy for code mode."""

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ):
        rejected = self._reject_switch_mode_exit_in_plan(ctx, tool_call)
        if rejected is not None:
            return rejected

        if user_input is None and tool_call is not None:
            tool_name = tool_call.name or ""
            tool_args = self._parse_tool_args(tool_call)
            message = build_confirm_interrupt_message(tool_name, tool_args)
            return self.interrupt(
                InterruptRequest(
                    message=message,
                    payload_schema=self.request.payload_schema,
                    auto_confirm_key=self._get_auto_confirm_key(tool_call),
                )
            )
        return await super().resolve_interrupt(
            ctx, tool_call, user_input, auto_confirm_config
        )

    def _reject_switch_mode_exit_in_plan(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
    ):
        if tool_call is None or (tool_call.name or "") != "switch_mode":
            return None
        agent = ctx.agent
        if agent is None:
            return None
        plan_state = agent.load_state(ctx.session).plan_mode
        if plan_state.mode != "plan":
            return None
        args = self._parse_tool_args(tool_call)
        target = str(args.get("mode") or args.get("target_mode") or "").strip()
        if target not in {"normal", "auto"}:
            return None
        language = "cn"
        builder = getattr(agent, "system_prompt_builder", None)
        if builder is not None and getattr(builder, "language", "cn") == "en":
            language = "en"
        msg = _SWITCH_MODE_EXIT_PLAN_MSG_EN if language == "en" else _SWITCH_MODE_EXIT_PLAN_MSG_CN
        return self.reject(tool_result={"error": msg})

    @staticmethod
    def _parse_tool_args(tool_call: ToolCall) -> dict[str, Any]:
        raw = getattr(tool_call, "arguments", None)
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}


__all__ = [
    "CodeConfirmInterruptRail",
    "build_confirm_interrupt_message",
]
