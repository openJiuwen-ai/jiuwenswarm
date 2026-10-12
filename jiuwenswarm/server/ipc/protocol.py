# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Length-prefixed JSON frames for Front ↔ Runtime Worker IPC.

The frame is a 4-byte big-endian length followed by UTF-8 JSON. Messages
carry a ``kind`` so one connection can multiplex request streams, cancel,
readiness, and server push.
"""

from __future__ import annotations

import json
import struct
from enum import Enum
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, PermissionContext
from jiuwenswarm.common.schema.message import ReqMethod

PROTOCOL_VERSION = 1
HEADER = struct.Struct("!I")
MAX_FRAME_BYTES = 32 * 1024 * 1024

KIND_HELLO = "hello"
KIND_REQUEST = "request"
KIND_REQUEST_DONE = "request.done"
KIND_WIRE = "wire"
KIND_READINESS = "readiness"
KIND_CANCEL = "cancel"
KIND_HEARTBEAT = "heartbeat"
KIND_SHUTDOWN = "shutdown"
KIND_GATEWAY_ATTACH = "gateway.attach"
KIND_GATEWAY_DETACH = "gateway.detach"
KIND_ERROR = "error"

_READINESS_STATES = frozenset(
    {"RUNTIME_WARMING", "AGENT_READY", "DEGRADED", "FAILED"}
)


class IpcError(Exception):
    """Broken or invalid IPC frame."""


def encode_frame(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise IpcError(f"ipc frame is {len(body)} bytes; limit is {MAX_FRAME_BYTES}")
    return HEADER.pack(len(body)) + body


def decode_frame(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_FRAME_BYTES:
        raise IpcError(f"ipc frame is {len(payload)} bytes; limit is {MAX_FRAME_BYTES}")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcError(f"ipc frame is not json: {exc}") from exc
    if not isinstance(data, dict):
        raise IpcError("ipc frame must be a JSON object")
    return data


def dump_agent_request(request: AgentRequest) -> dict[str, Any]:
    """JSON-safe view of an ``AgentRequest``. Does not import Agent Runtime."""
    method = request.req_method.value if request.req_method is not None else None
    permission = None
    if request.permission_context is not None:
        permission = request.permission_context.to_dict()
    return {
        "request_id": request.request_id,
        "channel_id": request.channel_id,
        "session_id": request.session_id,
        "chat_id": request.chat_id,
        "req_method": method,
        "params": request.params or {},
        "is_stream": bool(request.is_stream),
        "timestamp": request.timestamp,
        "metadata": request.metadata,
        "enable_memory": request.enable_memory,
        "permission_context": permission,
        "agent_ref": _dump_agent_ref(request.agent_ref),
        "user_id": request.user_id,
        "trusted_session_message_route": request.trusted_session_message_route,
    }


def load_agent_request(payload: dict[str, Any]) -> AgentRequest:
    """Rebuild an ``AgentRequest`` inside the Worker process."""
    raw_method = payload.get("req_method")
    method = ReqMethod(raw_method) if raw_method else None
    permission = None
    raw_permission = payload.get("permission_context")
    if isinstance(raw_permission, dict):
        permission = PermissionContext.from_dict(raw_permission)
    params = payload.get("params")
    return AgentRequest(
        request_id=str(payload.get("request_id") or ""),
        channel_id=str(payload.get("channel_id") or ""),
        session_id=payload.get("session_id"),
        chat_id=payload.get("chat_id"),
        req_method=method,
        params=params if isinstance(params, dict) else {},
        is_stream=bool(payload.get("is_stream")),
        timestamp=float(payload.get("timestamp") or 0.0),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        enable_memory=payload.get("enable_memory"),
        permission_context=permission,
        agent_ref=_load_agent_ref(payload.get("agent_ref")),
        user_id=str(payload.get("user_id") or ""),
        trusted_session_message_route=(
            payload.get("trusted_session_message_route")
            if isinstance(payload.get("trusted_session_message_route"), dict)
            else None
        ),
    )


def cancel_request(message: dict[str, Any]) -> AgentRequest:
    """Build the execution-owned cancel request from a ``cancel`` frame."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    request_id = str(message.get("request_id") or params.get("request_id") or "")
    session_id = message.get("session_id") or params.get("session_id")
    return AgentRequest(
        request_id=request_id or f"cancel-{session_id or 'session'}",
        channel_id=str(message.get("channel_id") or ""),
        session_id=str(session_id) if session_id else None,
        req_method=ReqMethod.CHAT_CANCEL,
        params={
            "session_id": session_id or "",
            "request_id": params.get("request_id") or "",
        },
        is_stream=False,
        metadata=message.get("metadata") if isinstance(message.get("metadata"), dict) else None,
    )


def _dump_agent_ref(agent_ref: Any) -> Any:
    if agent_ref is None:
        return None
    mode = getattr(agent_ref, "mode", None)
    ref_id = getattr(agent_ref, "id", None)
    if isinstance(mode, str) and ref_id is not None and not isinstance(agent_ref, dict):
        return {"mode": mode, "id": str(ref_id)}
    if isinstance(agent_ref, Enum):
        return agent_ref.value
    return agent_ref


def _load_agent_ref(value: Any) -> Any:
    if not isinstance(value, dict) or "mode" not in value or "id" not in value:
        return value
    from jiuwenswarm.gateway.routing.keys import AgentRef

    return AgentRef(mode=str(value.get("mode") or ""), id=str(value.get("id") or ""))
