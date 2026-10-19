# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 在 Turbo 总结发送成功后结束本轮，不再让模型重复总结。"""

from __future__ import annotations

import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
    SkillTurboDeliverySummaryRail,
    _is_tool_interrupt,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    clear_pending_ppt_delivery_summary,
    emit_pending_ppt_delivery_summary,
    set_pending_ppt_delivery_summary,
    take_pending_ppt_delivery_summary,
)

logger = logging.getLogger(__name__)


class FlashDeliverySummaryRail(SkillTurboDeliverySummaryRail):
    """复用 Turbo 总结发送入口，仅 Flash 使用确定性收尾。"""

    # SDK priority 降序执行，先发 tool_result / 改写 HITL，再处理总结。
    priority = JiuSwarmStreamEventRail.priority - 1

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = str(
            ctx.inputs.tool_name or getattr(ctx.inputs.tool_call, "name", "") or ""
        ).strip()
        if tool_name != "skill_acceleration_exec":
            return
        result = ctx.inputs.tool_result
        if (
            _is_tool_interrupt(ctx)
            or ctx.session is None
            or ctx.exception is not None
        ):
            clear_pending_ppt_delivery_summary()
            return
        result_failed = isinstance(result, dict) and result.get("success") is False
        if result_failed or getattr(result, "success", None) is False:
            clear_pending_ppt_delivery_summary()
            return

        # 既有发送接口只返回 bool；保留正文用于最终 answer，再原样交回该接口。
        # 取出与回填之间没有 await，缓存仍是当前任务的 ContextVar。
        summary = take_pending_ppt_delivery_summary()
        if not summary:
            return
        set_pending_ppt_delivery_summary(summary)
        if not await emit_pending_ppt_delivery_summary(ctx.session):
            return

        # 发送入口已完成标记校验和缓存消费；只在写流成功后正常结束本轮。
        ctx.request_force_finish({"output": summary, "result_type": "answer"})
        logger.info(
            "[FlashDeliverySummaryRail] 总结已发送，请求结束本轮，跳过后续模型调用"
        )
