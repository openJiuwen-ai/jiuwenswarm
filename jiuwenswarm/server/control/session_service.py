# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session query/list Control Service. Does not create, switch, or delete sessions."""

from __future__ import annotations

from jiuwenswarm.server.control.repositories.session_repository import (
    handle_session_request as _handle_session_request,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse


async def handle_session_request(request: AgentRequest) -> AgentResponse:
    # Existing Gateway RPC forwards metadata query objects unchanged. Keep the
    # audit result nested so its activity projection cannot replace call status.
    is_metadata = getattr(request.req_method, "value", None) == "session.get_metadata"
    if is_metadata and "llm_audit" in (request.params or {}):
        from jiuwenswarm.server.control.llm_audit_service import handle_llm_audit_request

        response = await handle_llm_audit_request(request)
        if response.ok:
            response.payload = {"llm_audit": response.payload}
        return response
    return await _handle_session_request(request)

__all__ = ["handle_session_request"]
