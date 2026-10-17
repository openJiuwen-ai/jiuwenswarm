# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Health and readiness Control Service owned by Front."""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.methods import CONTROL_HEALTH_METHODS
from jiuwenswarm.server.lifecycle import Readiness

__all__ = ["CONTROL_HEALTH_METHODS", "handle_readiness"]


def handle_readiness(request: AgentRequest, readiness: Readiness) -> AgentResponse:
    payload = readiness.snapshot()
    if request.req_method == ReqMethod.HEALTH_CHECK_GET_CONF:
        payload = {**payload, "ok": True}
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )
