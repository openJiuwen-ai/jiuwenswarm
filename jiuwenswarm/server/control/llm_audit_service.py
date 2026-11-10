# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read call audit metadata and bounded original blocks through history.get."""
from __future__ import annotations

import sqlite3

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.control.responses import build_error_response
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session import llm_audit


async def handle_llm_audit_request(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    query = params.get("llm_audit")
    try:
        if request.is_stream or not isinstance(query, dict):
            raise ValueError("Call audit reads require a unary query object")
        session_id = params.get("session_id") or request.session_id
        llm_audit.lc.validate_id(session_id)
        llm_audit.lc.guard(session_id)
        action = query.get("action", "list")
        if action == "list":
            data = await run_history_io(llm_audit.list_calls, session_id,
                                        cursor=query.get("cursor"), limit=query.get("limit", 30))
        elif action == "call":
            data = await run_history_io(llm_audit.get_call, session_id, query.get("call_id"))
        elif action == "parts":
            data = await run_history_io(llm_audit.get_parts, session_id, query.get("call_id"),
                                        collection=query.get("collection", "messages"),
                                        start=query.get("start", 0), limit=query.get("limit", 30))
        elif action == "read":
            data = await run_history_io(llm_audit.read_part, session_id, query.get("call_id"),
                                        collection=query.get("collection", "messages"), index=query.get("index", 0),
                                        offset=query.get("offset", 0), max_bytes=query.get("max_bytes", 65536))
        else:
            raise ValueError("Unknown call audit action")
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, llm_audit.lc.LifecycleError) as exc:
        return build_error_response(request, str(exc), code="LLM_AUDIT_FAILED")
    return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                         ok=True, payload=data, metadata=request.metadata)
