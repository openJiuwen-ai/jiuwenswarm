# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Emit tool call / result chunks from a built-in subagent's stream.

The Web subagent panel is fed by ``task_tool_events``, which mirrors a
synchronous delegation's stream chunks through the SDK ``ActivityProjector``.
The SDK ReAct loop itself only writes reasoning and answer chunks; the
``tool_call`` / ``tool_result`` chunks come from ``JiuSwarmStreamEventRail``,
which only the root agent and general-purpose subagent carry. Built-in
subagents (browser_agent, cua_agent) therefore showed only "thinking" in the
panel while their tools ran (issue #8637).

This rail emits just those two chunks, in the payload shape of
``JiuSwarmStreamEventRail``, and nothing else: none of that rail's abort,
interrupt, todo, or tool-argument rewriting behavior reaches the subagent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.ability_manager import resolve_tool_result_text
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import _infer_tool_result_error

logger = logging.getLogger(__name__)

# Fallback summary length when the model-facing text is unavailable; the
# projector truncates further to its own activity limit.
_MAX_RESULT_CHARS = 2000


def _arguments(tool_call: Any) -> Any:
    arguments = getattr(tool_call, "arguments", {})
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return arguments
    return arguments


class SubagentToolActivityRail(AgentRail):
    """Write ``tool_call`` / ``tool_result`` chunks for the subagent panel.

    Runs at low priority, like the SDK ``ToolTrackingRail``, so permission
    rails can reject a call and result-rewriting rails finish before the
    chunk is written. Emission failures never break the tool call.
    """

    priority = 5

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        session = ctx.session
        if session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_call = ctx.inputs.tool_call
        try:
            await session.write_stream(
                OutputSchema(
                    type="tool_call",
                    index=0,
                    payload={
                        "tool_call": {
                            "name": getattr(tool_call, "name", "") or ctx.inputs.tool_name,
                            "arguments": _arguments(tool_call),
                            "tool_call_id": getattr(tool_call, "id", ""),
                        }
                    },
                )
            )
        except Exception:  # noqa: BLE001 - panel emission must never break the tool
            logger.debug("subagent tool_call emit failed", exc_info=True)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        session = ctx.session
        if session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_call = ctx.inputs.tool_call
        result = ctx.inputs.tool_result
        try:
            if ctx.exception is not None:
                success = False
            else:
                success = getattr(result, "success", None)
                if not isinstance(success, bool):
                    # Plain dict / text results: same inference as the root's panel rows.
                    error = _infer_tool_result_error(result)
                    success = None if error is None else not error
            rendered = resolve_tool_result_text(ctx.inputs, ctx.exception)
            if rendered is None:
                rendered = str(result if result is not None else ctx.exception or "")
            payload: dict[str, Any] = {
                "tool_name": getattr(tool_call, "name", "") or ctx.inputs.tool_name,
                "tool_call_id": getattr(tool_call, "id", ""),
                "rendered_result": rendered[:_MAX_RESULT_CHARS],
            }
            if isinstance(success, bool):
                payload["success"] = success
            await session.write_stream(
                OutputSchema(type="tool_result", index=0, payload={"tool_result": payload})
            )
        except Exception:  # noqa: BLE001
            logger.debug("subagent tool_result emit failed", exc_info=True)
