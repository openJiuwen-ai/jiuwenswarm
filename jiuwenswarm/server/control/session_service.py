# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session query/list Control Service. Does not create or delete sessions."""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import build_error_response
from jiuwenswarm.server.runtime.gateway_adapter.session_adapter import SessionAdapter

CONTROL_SESSION_METHODS = frozenset(
    {
        ReqMethod.SESSION_LIST.value,
        ReqMethod.SESSION_GET_METADATA.value,
        ReqMethod.SESSION_PIN.value,
        ReqMethod.SESSION_COLOR_SET.value,
        ReqMethod.SESSION_PREVIEW.value,
    }
)

_ADAPTER = SessionAdapter()


async def handle_session_request(request: AgentRequest) -> AgentResponse:
    method = request.req_method.value if request.req_method is not None else ""
    if method not in CONTROL_SESSION_METHODS:
        return build_error_response(
            request, f"unsupported control session method: {method}", code="BAD_REQUEST"
        )
    return await _ADAPTER.handle(request)
