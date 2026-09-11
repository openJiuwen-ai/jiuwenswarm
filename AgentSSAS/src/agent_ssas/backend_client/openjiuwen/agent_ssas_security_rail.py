# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSASSecurityRail - 事件采集 Rail,上报给 AgentSSAS。"""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)
from openjiuwen.harness.rails.security.base_security_rail import (
    BaseSecurityRail,
    SecurityDecision,
    SecurityInterrupt,
    SecurityReject,
)

from agent_ssas.backend_client.openjiuwen.event_builder import EventBuilder
from agent_ssas.backend_client.openjiuwen.event_filter import EventFilter
from agent_ssas.backend_client.openjiuwen.event_reporter import EventReporter
from agent_ssas.backend_client.openjiuwen.extended_context import (
    ExtendedSecurityCheckContext,
)
from agent_ssas.backend_client.openjiuwen.id_manager import IDManager
from agent_ssas.core.framework.access_adapter.protocol import AgentSSASBackendProtocol

# 走 _run_and_apply() → run_security_check() 管线的 6 个事件
_PIPELINE_EVENTS: set[AgentCallbackEvent] = {
    AgentCallbackEvent.BEFORE_INVOKE,
    AgentCallbackEvent.AFTER_INVOKE,
    AgentCallbackEvent.BEFORE_MODEL_CALL,
    AgentCallbackEvent.AFTER_MODEL_CALL,
    AgentCallbackEvent.BEFORE_TOOL_CALL,
    AgentCallbackEvent.AFTER_TOOL_CALL,
}

# MODEL 事件集合:SecurityInterrupt 在这些事件上静默降级为 SecurityReject
_MODEL_EVENTS: set[AgentCallbackEvent] = {
    AgentCallbackEvent.BEFORE_MODEL_CALL,
    AgentCallbackEvent.AFTER_MODEL_CALL,
}


class AgentSSASSecurityRail(BaseSecurityRail):
    """事件采集 Rail:采集全部 13 个生命周期事件 + 安全检测事件,
    通过单一接口 report_event(raw_event) 将 raw_event(三层结构 dict)上报给 AgentSSAS。

    priority=80,低于 PermissionInterruptRail(90) 和 SafetyPromptRail(85),
    在其他安全 Rail 之后执行,可观察前序 Rail 的 deny 决策标志。

    0.1版本仅实现 6 个走管线的事件(支持决策),
    7 个直接覆写钩子的事件为后续版本拓展。
    """

    priority: int = 80

    supported_events: set[AgentCallbackEvent] = _PIPELINE_EVENTS

    def __init__(self, backend: AgentSSASBackendProtocol, policy_name: str = "observe_only") -> None:
        super().__init__()
        self._backend = backend
        # 创建各模块实例
        self._id_manager = IDManager()
        self._event_builder = EventBuilder(self._id_manager)
        self._event_filter = EventFilter()
        self._event_reporter = EventReporter(backend, policy_name)

    # ------------------------------------------------------------------
    # 覆写 _run_and_apply:构建 ExtendedSecurityCheckContext 并填充 ID 字段
    # (不修改 agent-core 的原始 _run_and_apply,详见第九章)
    # ------------------------------------------------------------------
    async def _run_and_apply(
        self, ctx: AgentCallbackContext, event: AgentCallbackEvent
    ) -> None:
        """覆写基类的 _run_and_apply,构建扩展的 SecurityCheckContext。"""
        # ID 管理模块:生成交互标识(int 类型自增序号)
        interaction_seq = self._id_manager._ensure_interaction_seq(ctx, event)
        llm_call_seq = self._id_manager._ensure_llm_call_seq(ctx, event)
        tool_call_seq = self._id_manager._ensure_tool_call_seq(ctx, event)
        subsession_id = self._id_manager._resolve_subsession_id(ctx)

        # _resolve_subject_id、_get_user_input、_get_auto_confirm_config 由基类
        # BaseSecurityRail 提供,在此子类中通过 self 调用复用基类实现
        subject_id = self._resolve_subject_id(ctx, event)
        user_input = self._get_user_input(ctx, subject_id)
        security_ctx = ExtendedSecurityCheckContext(
            callback_ctx=ctx,
            event=event,
            user_input=user_input,
            auto_confirm_config=self._get_auto_confirm_config(ctx),
            subject_id=subject_id,
            interaction_seq=interaction_seq,
            session_id=self._id_manager._resolve_session_id(ctx),
            agent_id=self._id_manager._resolve_agent_id(ctx),
            trace_id=self._id_manager._resolve_trace_id(ctx),
            context_id=self._id_manager._resolve_context_id(ctx),
            conversation_id=self._id_manager._resolve_conversation_id(ctx, event),
            tool_call_id=self._id_manager._resolve_tool_call_id(
                getattr(ctx.inputs, "tool_call", None)
            ),
            tool_name=self._id_manager._resolve_tool_name(ctx, event),
            llm_call_seq=llm_call_seq,
            tool_call_seq=tool_call_seq,
            subsession_id=subsession_id,
        )
        decision = await self.run_security_check(security_ctx)
        if isinstance(decision, SecurityInterrupt) and event in _MODEL_EVENTS:
            # MODEL 事件上 SecurityInterrupt 静默降级为 SecurityReject
            decision = SecurityReject(
                message=getattr(decision, "message", ""),
                result=getattr(decision, "result", None),
            )
        ctx.extra["_interrupt_decision"] = decision
        await self.apply_security_decision(security_ctx, decision)

    # ------------------------------------------------------------------
    # run_security_check:管线事件的核心处理
    # (采集模块 + 构建模块 + 事件过滤模块 + 上报模块)
    # ------------------------------------------------------------------
    async def run_security_check(
        self, security_ctx: ExtendedSecurityCheckContext
    ) -> SecurityDecision:
        """处理走管线的 6 个事件,采集并上报,同时检测安全检测事件。"""
        # 1. 构建模块:构建 raw_event(三层结构 dict:common/payload/metadata)
        event_dict = self._event_builder.build_event_dict(security_ctx)

        # 2. 事件过滤模块:针对事件类型和策略进行过滤
        filtered = self._event_filter.filter_event(event_dict, security_ctx)
        if filtered is None:
            # 被过滤掉,不上报,返回 Allow
            return self.allow()

        # 3. 上报模块:将 raw_event 上报给 AgentSSAS,获取 RiskAssessment,
        #    再映射为 SecurityDecision。fail-open 逻辑在 EventReporter.report 中:
        #    异常时返回 SecurityAllow()
        return await self._event_reporter.report(filtered)

    # ------------------------------------------------------------------
    # 采集模块:7 个直接覆写的事件(后续版本拓展,0.1版本未实现)
    # ------------------------------------------------------------------
    # async def on_user_message(self, ctx: AgentCallbackContext) -> None:
    #     """TODO: 直接覆写钩子,不走决策应用流程,不支持决策。"""
    #     event_dict = self._event_builder.build_event_dict(ctx, AgentCallbackEvent.ON_USER_MESSAGE)
    #     filtered = self._event_filter.filter_event(event_dict, ...)
    #     if filtered is not None:
    #         await self._event_reporter.safe_report(filtered)
    #
    # async def before_steering_drain(self, ctx: AgentCallbackContext) -> None:
    #     """TODO"""
    #     ...
    #
    # async def before_task_iteration(self, ctx: AgentCallbackContext) -> None:
    #     """TODO"""
    #     ...
    #
    # async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
    #     """TODO"""
    #     ...
    #
    # async def after_react_iteration(self, ctx: AgentCallbackContext) -> None:
    #     """TODO"""
    #     ...
    #
    # async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
    #     """TODO"""
    #     ...
    #
    # async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
    #     """TODO"""
    #     ...
