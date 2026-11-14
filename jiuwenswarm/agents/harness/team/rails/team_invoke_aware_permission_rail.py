# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team permission rail that adjudicates ``invoke_tool`` by its target tool.

``ProgressiveToolRail`` executes deferred tools via ``invoke_tool`` → direct
``target.invoke``, so the outer ``before_tool_call`` only sees ``invoke_tool``.
Without unwrapping, ASK-level targets (e.g. ``mcp_exec_command``) skip
leader-mediated approval. This subclass rewrites the call to the target for
``TeamPermissionRail`` adjudication, then restores the original inputs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from openjiuwen.agent_teams.rails.team_permission_rail import TeamPermissionRail
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import (
    PermissionInterruptRail,
)

logger = logging.getLogger(__name__)

_INVOKE_TOOL_NAME = "invoke_tool"


def _as_args_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def synthetic_target_tool_call_from_invoke(
    tool_call: Any,
    *,
    inputs_arguments: Any = None,
) -> Optional[ToolCall]:
    """Build a ToolCall for the invoke_tool target; None when target is missing."""
    args = PermissionInterruptRail.parse_tool_args(tool_call)
    if not args and inputs_arguments is not None:
        args = _as_args_dict(inputs_arguments)
    target_name = str(args.get("tool_name") or "").strip()
    if not target_name or target_name == _INVOKE_TOOL_NAME:
        return None
    target_args = _as_args_dict(args.get("arguments"))
    try:
        arguments_str = json.dumps(target_args, ensure_ascii=False)
    except Exception:
        arguments_str = "{}"
    call_id = str(getattr(tool_call, "id", "") or "") or None
    call_type = str(getattr(tool_call, "type", "") or "").strip() or "function"
    index = getattr(tool_call, "index", None) if tool_call is not None else None
    return ToolCall(
        id=call_id,
        type=call_type,
        name=target_name,
        arguments=arguments_str,
        index=index,
    )


class TeamInvokeAwarePermissionRail(TeamPermissionRail):
    """TeamPermissionRail that judges ``invoke_tool`` by its proxied target."""

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        outer_name = str(getattr(inputs, "tool_name", "") or "").strip()
        if inputs is None or outer_name != _INVOKE_TOOL_NAME:
            await super().before_tool_call(ctx)
            return

        synthetic = synthetic_target_tool_call_from_invoke(
            getattr(inputs, "tool_call", None),
            inputs_arguments=getattr(inputs, "arguments", None),
        )
        if synthetic is None:
            # Malformed invoke_tool: still run base check on the outer name
            # (typically ALLOW) and let invoke_tool itself reject empty targets.
            await super().before_tool_call(ctx)
            return

        original_name = inputs.tool_name
        original_call = inputs.tool_call
        inputs.tool_name = synthetic.name
        inputs.tool_call = synthetic
        logger.info(
            "[TeamPermission] invoke_tool unwrap for adjudication "
            "target=%s call_id=%s",
            synthetic.name,
            synthetic.id,
        )
        try:
            await super().before_tool_call(ctx)
        finally:
            inputs.tool_name = original_name
            inputs.tool_call = original_call


__all__ = [
    "TeamInvokeAwarePermissionRail",
    "synthetic_target_tool_call_from_invoke",
]
