# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patch: ensure _skip_tool rails attach a ToolMessage before execution short-circuit."""

from __future__ import annotations

_SKIP_TOOL_TOOL_MESSAGE_PATCHED = False


def apply_skip_tool_tool_message_patch() -> None:
    """Ensure _skip_tool rails always attach a ToolMessage before execution short-circuit."""
    global _SKIP_TOOL_TOOL_MESSAGE_PATCHED
    if _SKIP_TOOL_TOOL_MESSAGE_PATCHED:
        return
    try:
        from openjiuwen.core.foundation.llm import ToolMessage
        from openjiuwen.core.single_agent.ability_manager import AbilityManager
        from openjiuwen.core.single_agent.rail.base import ToolCallInputs
    except ImportError:
        return

    _orig = AbilityManager._railed_execute_single_tool_call  # pylint: disable=protected-access

    async def _patched_railed_execute_single_tool_call(self, ctx, tool_call, session, tag=None):
        if ctx.extra.get("_skip_tool") and isinstance(ctx.inputs, ToolCallInputs):
            if ctx.inputs.tool_msg is None:
                tc = ctx.inputs.tool_call or tool_call
                tool_call_id = getattr(tc, "id", "") if tc is not None else ""
                content = str(ctx.inputs.tool_result or "")
                ctx.inputs.tool_msg = ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                )
        return await _orig(self, ctx, tool_call, session, tag=tag)

    AbilityManager._railed_execute_single_tool_call = _patched_railed_execute_single_tool_call  # pylint: disable=protected-access
    _SKIP_TOOL_TOOL_MESSAGE_PATCHED = True
