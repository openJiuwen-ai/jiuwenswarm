# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""聊天链路兜底判定：哪些方法可以落到 chat 执行器。

``_process_message`` / ``_process_message_stream`` 在每个专属路由都没命中时，
原本会把请求当成用户聊天内容执行。枚举里新增了方法却忘了接 handler 时，
请求会静默变成一次聊天；这里把该行为显式化：只有聊天入口方法可以继续往下，
其余已知方法统一回 ``METHOD_NOT_FOUND``。

协议层后续可能把 ``req_method`` 传成字符串（``e2a.Message`` 支持 str），
所以比较一律按 wire value 进行。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

# 唯一允许落入 chat 执行链路的方法。带专属分支的 chat.answer /
# chat.cancel / chat.swarmflow_reply 不在这里，它们在前面就分流了。
CHAT_TURN_METHOD_VALUES: frozenset[str] = frozenset(
    {
        ReqMethod.CHAT_SEND.value,
        ReqMethod.CHAT_RESUME.value,
        # 流式 command.goal 的 set/resume 会继续落到 chat 链路。
        ReqMethod.COMMAND_GOAL.value,
    }
)

METHOD_NOT_FOUND_CODE = "METHOD_NOT_FOUND"


def req_method_value(method: Any) -> Any:
    """取用于比较和回包的 method 值，兼容枚举与纯字符串。"""
    return getattr(method, "value", method)


def is_chat_turn_method(method: Any) -> bool:
    """内部合成请求不携带 req_method，继续按聊天放行。"""
    value = req_method_value(method)
    return value is None or value in CHAT_TURN_METHOD_VALUES


def method_not_found_payload(method: Any) -> dict[str, Any]:
    return {
        "error": f"unknown method: {req_method_value(method)}",
        "code": METHOD_NOT_FOUND_CODE,
    }


def method_not_found_response(request: AgentRequest) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=False,
        payload=method_not_found_payload(request.req_method),
        metadata=request.metadata,
    )
