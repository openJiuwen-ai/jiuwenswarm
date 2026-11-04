# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``client_tool.*``: the one interface a client uses to run tools for the agent.

- ``client_tool.register`` ``{session_id, client_id, tools, replace?}``: declare tools.
- ``client_tool.result`` ``{session_id, call_id, result, client_id?}``: answer a call.
- ``client_tool.cancel`` ``{session_id, call_id, reason?, client_id?}``: give a call up.
- ``client_tool.list_pending`` ``{session_id, client_id?}``: the calls still waiting, as
  ``chat.client_tool_dispatch`` events (a reconnecting client runs them).

See ``jiuwenswarm.runtime.client_tool_adapter``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.common.cancel_sources import CANCEL_SOURCE_NEW_MESSAGE
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.client_tool_adapter import (
    SESSION_NOT_FOUND,
    ClientToolAdapter,
    ClientToolCall,
    ClientToolError,
    parse_tools_param,
    specs_wire,
    valid_session_id,
)
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

UNAVAILABLE = "CLIENT_TOOL_UNAVAILABLE"


def _session_metadata(session_id: str) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
    return metadata if isinstance(metadata, dict) else {}


async def _authorize(session_id: str, user_id: Any) -> None:
    """Refuse unless the Session exists and the requester owns it.

    The same rule as cross-Session messaging: a request naming a user matches
    only that user's Sessions, an anonymous one only Sessions nobody owns.
    Every refusal reads as "not found", so it never tells whether a Session exists.
    """
    metadata = await asyncio.to_thread(_session_metadata, session_id)
    stored = str(metadata.get("user_id") or "").strip()
    requested = str(user_id or "").strip()
    if not metadata or (stored != requested if requested else bool(stored)):
        raise ClientToolError(SESSION_NOT_FOUND, "session not found")


def _call_wire(call: ClientToolCall) -> dict[str, Any]:
    return {
        "call_id": call.call_id,
        "status": call.status.value,
        "reason": call.reason,
        "delivery": call.delivery.value,
    }


async def _dispatch(adapter: ClientToolAdapter, method: Any, params: dict[str, Any], session_id: str) -> dict:
    client_id = str(params.get("client_id") or "").strip()
    if method == ReqMethod.CLIENT_TOOL_REGISTER:
        specs = await adapter.register(
            session_id, client_id, parse_tools_param(params.get("tools")),
            replace_owned=params.get("replace") is True,
        )
        return {"session_id": session_id, "tools": specs_wire(specs), "pending": adapter.list_pending(session_id)}
    if method == ReqMethod.CLIENT_TOOL_RESULT:
        if "result" not in params:
            raise ClientToolError("CLIENT_TOOL_BAD_REQUEST", "result is required")
        call = await adapter.submit_result(session_id, params.get("call_id"), params["result"], client_id=client_id)
        return {"session_id": session_id, **_call_wire(call)}
    if method == ReqMethod.CLIENT_TOOL_CANCEL:
        reason = params.get("reason")
        call = await adapter.cancel(
            session_id, params.get("call_id"), reason=reason if isinstance(reason, str) else "", client_id=client_id,
        )
        return {"session_id": session_id, **_call_wire(call)}
    if session_id:
        adapter.on_client_attached(session_id, client_id)
    return {"session_id": session_id, "pending": adapter.list_pending(session_id) if session_id else []}


async def settle_client_tools_on_chat_cancel(runtime: Any, request: AgentRequest) -> None:
    """Apply a ``chat.cancel`` to the Session's running client tool calls.

    - A client that disconnected gets the grace period to come back before its
      calls end as ``client_gone``.
    - The user stopping the conversation cancels them.
    - A new message replacing the running turn, a pause or a supplement leaves
      them running: the user is still in the conversation.
    """
    adapter = getattr(runtime, "client_tool_adapter", None)
    session_id = request.session_id or "default"
    if adapter is None:
        return
    params = request.params if isinstance(request.params, dict) else {}
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    source = str(metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY) or "").strip()
    try:
        if source == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT:
            adapter.on_client_detached(session_id)
        elif params.get("intent", "cancel") == "cancel" and source != CANCEL_SOURCE_NEW_MESSAGE:
            await adapter.cancel_session(session_id, reason="the user stopped the conversation")
    except Exception:  # noqa: BLE001 - cancelling the turn must go on
        logger.warning("[ClientTools] chat.cancel not applied: session_id=%s", session_id, exc_info=True)


async def handle_client_tool_request(ctx: RequestContext) -> None:
    """Serve one ``client_tool.*`` request."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    adapter = getattr(ctx.services.execution_runtime(), "client_tool_adapter", None)
    try:
        if adapter is None:
            raise ClientToolError(UNAVAILABLE, "client tools are not available on this server")
        valid_session_id(session_id)
        await _authorize(session_id, request.user_id)
        ok, payload = True, await _dispatch(adapter, request.req_method, params, session_id)
    except ClientToolError as exc:
        ok, payload = False, {"error": str(exc), "code": exc.code}
    except Exception as exc:  # noqa: BLE001
        logger.exception("[ClientTools] %s failed", request.req_method)
        ok, payload = False, {"error": str(exc), "code": "CLIENT_TOOL_INTERNAL"}
    await ctx.sink.send_response(AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
    ))


__all__ = ["handle_client_tool_request", "settle_client_tools_on_chat_cancel"]
