# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EventBuilder - 构建模块,将上下文数据转换为 raw_event(三层结构 dict)。"""

from __future__ import annotations

import time
from typing import Any

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)

from agent_ssas.backend_client.openjiuwen.extended_context import (
    ExtendedSecurityCheckContext,
)
from agent_ssas.backend_client.openjiuwen.id_manager import IDManager


class EventBuilder:
    """构建模块。

    将 AgentCallbackContext 和 ExtendedSecurityCheckContext 中的数据
    转换为 raw_event(三层结构 dict:common/payload/metadata),
    即 AgentSSASSecurityRail 传递给 AgentSSAS 的事件对象。
    common 层包含 14 个通用字段。
    """

    def __init__(self, id_manager: IDManager) -> None:
        self._id_manager = id_manager

    def build_event_dict(
        self, security_ctx: ExtendedSecurityCheckContext
    ) -> dict[str, Any]:
        """构建管线事件的 raw_event(三层结构 dict:common/payload/metadata)。"""
        ctx = security_ctx.callback_ctx
        event = security_ctx.event

        # common 层(14 个通用字段,tool_call_id 在 common 层,
        # interaction_seq/llm_call_seq 为整数自增序号;llm_call_seq 在 tool_call 事件中
        # 为工具调用所属的大模型调用序号)
        event_type = self._event_type_for(event)
        common: dict[str, Any] = {
            "source": "AgentSSASSecurityRail",
            "event_type": event_type,
            "event_class": self._id_manager._event_class_for(event, event_type),
            "timestamp": time.time(),
            "interaction_seq": security_ctx.interaction_seq,
            "session_id": security_ctx.session_id,
            "conversation_id": security_ctx.conversation_id,
            "agent_id": security_ctx.agent_id,
            "trace_id": security_ctx.trace_id,
            "context_id": security_ctx.context_id,
            "llm_call_seq": security_ctx.llm_call_seq,
            "tool_call_seq": security_ctx.tool_call_seq,
            "subsession_id": security_ctx.subsession_id,
            "tool_call_id": security_ctx.tool_call_id,
        }

        # payload 层(tool_call_id 在 common 层)
        payload: dict[str, Any] = {}
        content = self._extract_content(ctx, event)
        if content:
            payload["content"] = content
        if security_ctx.tool_name:
            payload["tool_name"] = security_ctx.tool_name
        if ctx.exception is not None:
            payload["exception"] = str(ctx.exception)

        # metadata 层
        metadata: dict[str, Any] = {}

        return {"common": common, "payload": payload, "metadata": metadata}

    def _event_type_for(self, event: AgentCallbackEvent) -> str:
        """将 AgentCallbackEvent 映射为 event_type 字符串。"""
        mapping = {
            AgentCallbackEvent.BEFORE_INVOKE: "invoke_start",
            AgentCallbackEvent.AFTER_INVOKE: "invoke_end",
            AgentCallbackEvent.ON_USER_MESSAGE: "user_message",
            AgentCallbackEvent.BEFORE_STEERING_DRAIN: "steering_drain",
            AgentCallbackEvent.BEFORE_TASK_ITERATION: "task_iteration_start",
            AgentCallbackEvent.AFTER_TASK_ITERATION: "task_iteration_end",
            AgentCallbackEvent.BEFORE_MODEL_CALL: "llm_input",
            AgentCallbackEvent.AFTER_MODEL_CALL: "llm_output",
            AgentCallbackEvent.ON_MODEL_EXCEPTION: "model_exception",
            AgentCallbackEvent.BEFORE_TOOL_CALL: "tool_input",
            AgentCallbackEvent.AFTER_TOOL_CALL: "tool_output",
            AgentCallbackEvent.ON_TOOL_EXCEPTION: "tool_exception",
            AgentCallbackEvent.AFTER_REACT_ITERATION: "react_iteration_end",
        }
        return mapping.get(event, event.value)

    def _extract_content(
        self, ctx: AgentCallbackContext, event: AgentCallbackEvent
    ) -> dict[str, Any]:
        """按事件类型提取交互内容类字段。"""
        content: dict[str, Any] = {}
        inputs = ctx.inputs

        if event == AgentCallbackEvent.BEFORE_INVOKE:
            content["query"] = getattr(inputs, "query", None)
            content["parent_session_id"] = getattr(inputs, "parent_session_id", None)
            content["run_kind"] = str(getattr(inputs, "run_kind", None))
        elif event == AgentCallbackEvent.AFTER_INVOKE:
            content["result"] = getattr(inputs, "result", None)
        elif event == AgentCallbackEvent.ON_USER_MESSAGE:
            content["parts"] = getattr(inputs, "parts", [])
            content["source"] = getattr(inputs, "source", "")
        elif event == AgentCallbackEvent.BEFORE_TASK_ITERATION:
            content["iteration"] = getattr(inputs, "iteration", 0)
            content["query"] = getattr(inputs, "query", None)
            content["is_follow_up"] = getattr(inputs, "is_follow_up", False)
        elif event == AgentCallbackEvent.AFTER_TASK_ITERATION:
            content["iteration"] = getattr(inputs, "iteration", 0)
            content["result"] = getattr(inputs, "result", None)
        elif event == AgentCallbackEvent.BEFORE_MODEL_CALL:
            content["messages"] = getattr(inputs, "messages", [])
            content["tools"] = getattr(inputs, "tools", None)
        elif event == AgentCallbackEvent.AFTER_MODEL_CALL:
            content["response"] = getattr(inputs, "response", None)
        elif event == AgentCallbackEvent.BEFORE_TOOL_CALL:
            content["tool_args"] = getattr(inputs, "tool_args", None)
        elif event == AgentCallbackEvent.AFTER_TOOL_CALL:
            content["tool_result"] = getattr(inputs, "tool_result", None)

        return content
