# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway → AgentServer long-horizon calls on the existing E2A WebSocket (I2b / I2c)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from jiuwenswarm.gateway.long_horizon.job_tags import STAGE_DUE_EVENT
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)


def resolve_agent_client(context: Any = None) -> Any | None:
    client = getattr(context, "agent_client", None) if context is not None else None
    if client is not None:
        return client
    mh = getattr(context, "message_handler", None) if context is not None else None
    if mh is not None and getattr(mh, "agent_client", None) is not None:
        return mh.agent_client
    try:
        from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

        inst = MessageHandler.get_instance()
        return getattr(inst, "agent_client", None)
    except Exception:
        return None


async def call_agent_long_horizon(
    *,
    action: str,
    params: dict[str, Any] | None = None,
    channel_id: str = "web",
    session_id: str | None = None,
    agent_client: Any | None = None,
    context: Any = None,
) -> dict[str, Any]:
    """Unary ``long_horizon`` request on the AgentServer WebSocket. Does not read task JSON."""
    client = agent_client or resolve_agent_client(context)
    if client is None:
        return {
            "success": False,
            "error": "agent_client_unavailable",
            "message": "Gateway 无法联系 AgentServer，不能改长程任务状态。",
        }
    body = dict(params or {})
    body["action"] = action
    request_id = f"lh_{action}_{uuid.uuid4().hex[:10]}"
    envelope = e2a_from_agent_fields(
        request_id=request_id,
        channel_id=channel_id or "web",
        session_id=session_id,
        req_method=ReqMethod.LONG_HORIZON,
        params=body,
        is_stream=False,
        timestamp=time.time(),
        metadata={"source": "long_horizon"},
    )
    try:
        resp = await client.send_request(envelope)
    except Exception as exc:
        logger.warning("[long_horizon] agent call failed action=%s: %s", action, exc)
        return {"success": False, "error": str(exc)}
    payload = dict(resp.payload or {}) if isinstance(resp.payload, dict) else {}
    if "success" not in payload:
        payload["success"] = bool(getattr(resp, "ok", False))
    if not getattr(resp, "ok", True) and not payload.get("error"):
        payload["error"] = "long_horizon_call_failed"
    return payload


async def broadcast_stage_due(
    message_handler: Any,
    payload: dict[str, Any],
    *,
    channel_id: str = "web",
) -> None:
    """I1d: Gateway broadcasts toast fields returned by Agent mark_due."""
    if message_handler is None:
        return
    session_id = str(payload.get("exec_session_id") or "")
    request_id = (
        f"lh_{payload.get('task_id')}_{payload.get('stage_id')}_{uuid.uuid4().hex[:8]}"
    )
    frame = {
        **payload,
        "event_type": STAGE_DUE_EVENT,
        "session_id": session_id,
        "request_id": request_id,
        "is_final": True,
        "is_complete": True,
    }
    channel_mgr = getattr(message_handler, "channel_manager", None) or getattr(
        message_handler, "_channel_manager", None
    )
    if channel_mgr is None:
        return
    try:
        web = (
            channel_mgr.get_channel(channel_id)
            if hasattr(channel_mgr, "get_channel")
            else None
        )
    except Exception:
        web = None
    if web is None:
        return
    if hasattr(web, "broadcast_event"):
        await web.broadcast_event(STAGE_DUE_EVENT, frame)
        return
    if hasattr(web, "send"):
        from jiuwenswarm.common.schema.message import Message

        msg = Message(
            id=request_id,
            type="event",
            channel_id=channel_id,
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload=frame,
            event_type=STAGE_DUE_EVENT,
        )
        await web.send(msg)
