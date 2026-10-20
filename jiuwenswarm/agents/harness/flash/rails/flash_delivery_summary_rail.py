# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 在交付历史落盘、整批工具完成且任务明确结束后收尾。"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
    SkillTurboDeliverySummaryRail,
    _is_tool_interrupt,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    _PPT_DELIVERY_SUMMARY_POST_TOOL_HINT,
    clear_pending_ppt_delivery_summary,
    emit_pending_ppt_delivery_summary,
    set_pending_ppt_delivery_summary,
    take_pending_ppt_delivery_summary,
)

logger = logging.getLogger(__name__)

_TOOL_NAME = "skill_acceleration_exec"
_FINISH_PARAMETER = "finish_request"
_STATE_KEY = "flash_delivery_state"
_TOKEN_KEY = "flash_delivery_state_token"
_DELIVERY_HINT = (
    "\n\n[SYSTEM] This tool's delivery summary has already been sent to the user. "
    "Do not repeat this summary or resend this artifact. This completes only this "
    "tool's task, not necessarily the entire request. Continue any remaining "
    "artifacts or other tasks, using the tools actually provided. Call Turbo "
    "serially for separate artifacts; set finish_request=true only for the final "
    "delivery when no further work remains."
)


@dataclass
class _DeliveryState:
    """一次请求内共享；并行工具继承同一对象，不保存到 rail 实例。"""

    pending: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    finish_call_id: str = ""
    completed_call_id: str = ""
    context: Any = None
    context_engine: Any = None


_delivery_state: ContextVar[_DeliveryState | None] = ContextVar(
    "flash_delivery_state", default=None,
)


class FlashDeliverySummaryRail(SkillTurboDeliverySummaryRail):
    """复用 Turbo 总结发送入口，仅 Flash 使用确定性收尾。"""

    # SDK priority 降序执行，先发 tool_result / 改写 HITL，再处理总结。
    priority = JiuSwarmStreamEventRail.priority - 1

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        state = _DeliveryState()
        ctx.extra[_STATE_KEY] = state
        ctx.extra[_TOKEN_KEY] = _delivery_state.set(state)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ModelCallInputs):
            return
        # 只改本次模型输入副本，不改变公共 Turbo 的工具卡或调用签名。
        for index, tool in enumerate(ctx.inputs.tools or []):
            if tool.name != _TOOL_NAME:
                continue
            local_tool = tool.model_copy(deep=True)
            if not isinstance(local_tool.parameters, dict):
                continue
            properties = local_tool.parameters.setdefault("properties", {})
            properties[_FINISH_PARAMETER] = {
                "type": "boolean",
                "default": False,
                "description": (
                    "Only set true when this delivery completes the ENTIRE user request, "
                    "with no remaining artifact, edit, todo update, or other task. "
                    "For multiple artifacts, call serially and keep false until the last "
                    "delivery. Omitted/false means continue after this tool; do not repeat "
                    "the delivery summary already sent by the system."
                ),
            }
            ctx.inputs.tools[index] = local_tool

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        state = _delivery_state.get()
        if state is None or not isinstance(ctx.inputs, ModelCallInputs):
            return
        state.finish_call_id = ""
        state.completed_call_id = ""
        calls = getattr(ctx.inputs.response, "tool_calls", None) or []
        for call in calls:
            if call.name != _TOOL_NAME:
                continue
            finish = self._take_finish_parameter(call)
            # 混合批次不作整个请求已完成的推断；由模型继续处理其它结果。
            if finish and len(calls) == 1:
                state.finish_call_id = call.id

    @staticmethod
    def _take_finish_parameter(call: Any) -> bool:
        """在工具调用落入 SDK 历史和执行前剥离 Flash 专用参数。"""
        arguments = call.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return False  # 非法参数继续交给 SDK 的既有校验。
        if not isinstance(arguments, dict) or _FINISH_PARAMETER not in arguments:
            return False
        arguments = dict(arguments)
        finish = arguments.pop(_FINISH_PARAMETER) is True
        call.arguments = json.dumps(arguments, ensure_ascii=False)
        return finish

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        tool_name = str(
            ctx.inputs.tool_name or getattr(ctx.inputs.tool_call, "name", "") or ""
        ).strip()
        if tool_name != _TOOL_NAME:
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

        # 既有发送接口只返回 bool；保留正文用于模型历史，再原样交回该接口。
        # 取出与回填之间没有 await，缓存仍是当前任务的 ContextVar。
        summary = take_pending_ppt_delivery_summary()
        if not summary:
            return
        set_pending_ppt_delivery_summary(summary)
        if not await emit_pending_ppt_delivery_summary(ctx.session):
            return
        self._scope_delivery_hint(ctx.inputs)

        state = _delivery_state.get()
        if state is not None:
            state.pending.append(summary)
            state.context = ctx.context
            state.context_engine = getattr(ctx.agent, "context_engine", None)
            state.completed_call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")

    @staticmethod
    def _scope_delivery_hint(inputs: ToolCallInputs) -> None:
        """共享停止提示只适用于当前产物，不能禁止同一请求的下一次交付。"""
        result = inputs.tool_result
        if not isinstance(result, dict):
            return
        content = result.get("result")
        if not isinstance(content, str):
            return
        for hint in _PPT_DELIVERY_SUMMARY_POST_TOOL_HINT.values():
            content = content.replace(hint, _DELIVERY_HINT)
        inputs.tool_result = {**result, "result": content}
        if inputs.tool_msg is not None:
            inputs.tool_msg = inputs.tool_msg.model_copy(update={"content": str(inputs.tool_result)})

    async def after_react_iteration(self, ctx: AgentCallbackContext) -> None:
        # 此时所有 ToolMessage 已追加，SDK 也已检查整批 HITL / workflow 中断。
        state = _delivery_state.get()
        if state is None or not await self._append_history(state):
            return
        if not state.finish_call_id or state.finish_call_id != state.completed_call_id:
            return
        if not ctx.close_steering_if_empty():
            return
        ctx.request_force_finish({
            "output": "\n\n".join(state.delivered), "result_type": "answer",
        })
        logger.info("[FlashDeliverySummaryRail] 全部任务交付完成，历史已追加，批次结束后收尾")

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        state = ctx.extra.pop(_STATE_KEY, None)
        token = ctx.extra.pop(_TOKEN_KEY, None)
        try:
            # 中断不触发 after_react_iteration；保留已成功交付的其它工具总结。
            # DeepAgent 的 after_invoke 在外层，用请求内捕获的内层 context 写回。
            if state is not None and await self._append_history(state):
                if state.context_engine is not None:
                    await state.context_engine.save_contexts(ctx.session)
        finally:
            if token is not None:
                _delivery_state.reset(token)

    @staticmethod
    async def _append_history(state: _DeliveryState) -> bool:
        if state.context is None or not state.pending:
            return False
        # 已交付工具的 ToolMessage 必须先写入；待审批工具沿用原生恢复/修复链。
        for summary in list(state.pending):
            await state.context.add_messages(AssistantMessage(content=summary))
            state.delivered.append(summary)
            state.pending.remove(summary)
        return True
