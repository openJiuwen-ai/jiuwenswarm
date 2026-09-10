# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboDeliverySummaryRail — 外层 tool_result 之后发出 PPT 交付总结。

将 PPT 交付总结投递从通用 StreamEventRail 解耦：
P10 成功时 skill_turbo_tools 把骨架挂到 ContextVar；本 rail 在
skill_acceleration_exec 的 after_tool_call 中、且晚于 StreamEventRail
发出 tool_result 之后，再 write_stream(llm_output)。

时序约束（openjiuwen：priority 越小越先执行）：
StreamEventRail.priority=80 先发 tool_result；本 rail priority=90 后发骨架，
才能落入 RelayClaw 的 pptTurboSummary 收集窗口。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_SKILL_ACCELERATION_TOOL = "skill_acceleration_exec"


class SkillTurboDeliverySummaryRail(DeepAgentRail):
    """在 skill_acceleration_exec 成功结束后流式发出待投递的 PPT 交付总结。"""

    # 必须 > StreamEventRail(80)，保证 tool_result 已发出后再发骨架。
    priority = 90

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return

        tool_name = str(
            getattr(ctx.inputs, "tool_name", None)
            or getattr(getattr(ctx.inputs, "tool_call", None), "name", "")
            or ""
        ).strip()
        if tool_name != _SKILL_ACCELERATION_TOOL:
            return

        try:
            from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                clear_pending_ppt_delivery_summary,
                emit_pending_ppt_delivery_summary,
            )
        except Exception:
            logger.debug(
                "[SkillTurboDeliverySummaryRail] import delivery summary helpers failed",
                exc_info=True,
            )
            return

        # HITL / 审批中断：尚无真实 tool_result，清掉可能残留的 pending，勿发成功骨架。
        if _is_tool_interrupt(ctx):
            clear_pending_ppt_delivery_summary()
            return

        session = getattr(ctx, "session", None)
        if session is None:
            clear_pending_ppt_delivery_summary()
            logger.debug(
                "[SkillTurboDeliverySummaryRail] skip emit: session is None"
            )
            return

        try:
            await emit_pending_ppt_delivery_summary(session)
        except Exception:
            logger.debug(
                "[SkillTurboDeliverySummaryRail] emit after tool_result failed",
                exc_info=True,
            )


def _is_tool_interrupt(ctx: AgentCallbackContext) -> bool:
    try:
        from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
            extract_tool_interrupt,
        )
    except Exception:
        return False

    tool_result = getattr(ctx.inputs, "tool_result", None) if ctx.inputs else None
    if extract_tool_interrupt(tool_result) is not None:
        return True
    if extract_tool_interrupt(getattr(ctx, "exception", None)) is not None:
        return True
    return False


__all__ = ["SkillTurboDeliverySummaryRail"]
