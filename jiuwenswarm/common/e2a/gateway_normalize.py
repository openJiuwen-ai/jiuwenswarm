# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``e2a.gateway_normalize``。

本模块保留 ``jiuwenswarm.common.e2a.gateway_normalize`` 旧 import 路径兼容；多数符号 re-export
e2a 协议包 同一对象。``e2a_response_from_agent_chunk`` / ``e2a_response_to_agent_chunk``
在本仓包装以保留 Team/组织 delta 身份字段（enterprise）。
请求转换先将本仓 ReqMethod 转为字符串，再交给协议包处理。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from e2a import gateway_normalize as protocol_normalize
from e2a.agent_models import AgentResponseChunk
from e2a.models import E2AResponse
from e2a.gateway_normalize import (  # noqa: F401
    E2AEnvelope,
    E2AProvenance,
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
    E2A_PROTOCOL_VERSION,
    E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST,
    E2A_RESPONSE_KIND_CRON,
    E2A_RESPONSE_KIND_E2A_CHUNK,
    E2A_RESPONSE_KIND_E2A_COMPLETE,
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_RESPONSE_KIND_PLAN_APPROVAL_REQUIRED,
    E2A_RESPONSE_STATUS_FAILED,
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_RESPONSE_STATUS_SUCCEEDED,
    E2A_SOURCE_PROTOCOL_E2A,
    IdentityOrigin,
    MAX_LEGACY_AGENT_REQUEST_JSON_BYTES,
    build_fallback_e2a,
    channel_context_for_channel_reply,
    e2a_response_from_agent_response,
    e2a_response_to_agent_response,
    utc_now_iso,
)

from jiuwenswarm.common.schema.message import Message, ReqMethod


def _wire_message(msg: Message) -> Message:
    """Pass method strings to E2A without mutating the caller's message."""
    if isinstance(msg.req_method, ReqMethod):
        return replace(msg, req_method=msg.req_method.value)
    return msg


def message_to_e2a(msg: Message) -> E2AEnvelope:
    return protocol_normalize.message_to_e2a(_wire_message(msg))


def message_to_e2a_or_fallback(msg: Message) -> E2AEnvelope:
    return protocol_normalize.message_to_e2a_or_fallback(_wire_message(msg))


def message_to_legacy_agent_dict(msg: Message) -> dict[str, Any]:
    return protocol_normalize.message_to_legacy_agent_dict(_wire_message(msg))


def e2a_from_agent_fields(
    *,
    request_id: str,
    channel_id: str = "",
    session_id: str | None = None,
    req_method: ReqMethod | str | None = None,
    params: dict[str, Any] | None = None,
    is_stream: bool = False,
    timestamp: float = 0.0,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> E2AEnvelope:
    """Encode local methods as strings at the protocol boundary."""
    return protocol_normalize.e2a_from_agent_fields(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        req_method=req_method.value if isinstance(req_method, ReqMethod) else req_method,
        params=params,
        is_stream=is_stream,
        timestamp=timestamp,
        metadata=metadata,
        user_id=user_id,
    )


def copy_team_delta_identity(source: dict[str, Any], target: dict[str, Any]) -> None:
    """Preserve Team routing and the shared organization reply ID on text deltas."""
    for key in ("team_name", "team_id"):
        value = source.get(key)
        if value is not None:
            target[key] = value
    if str(source.get("source", "")).startswith("org_") and "request_id" in source:
        target["request_id"] = source["request_id"]


def e2a_response_from_agent_chunk(
    chunk: AgentResponseChunk,
    *,
    response_id: str,
    sequence: int,
    is_stream: bool = True,
    timestamp: str | None = None,
) -> E2AResponse:
    """Delegate normalization while retaining host Team delta fields."""
    response = protocol_normalize.e2a_response_from_agent_chunk(
        chunk,
        response_id=response_id,
        sequence=sequence,
        is_stream=is_stream,
        timestamp=timestamp,
    )
    if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.delta":
        copy_team_delta_identity(chunk.payload, response.body)
    return response


def e2a_response_to_agent_chunk(e2a: E2AResponse) -> AgentResponseChunk:
    """Delegate restoration while retaining host Team delta fields."""
    chunk = protocol_normalize.e2a_response_to_agent_chunk(e2a)
    if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.delta":
        copy_team_delta_identity(e2a.body, chunk.payload)
    return chunk


__all__ = [
    "E2AEnvelope",
    "E2AProvenance",
    "E2AResponse",
    "E2A_FALLBACK_FAILED_KEY",
    "E2A_INTERNAL_CONTEXT_KEY",
    "E2A_LEGACY_AGENT_REQUEST_KEY",
    "E2A_PROTOCOL_VERSION",
    "E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST",
    "E2A_RESPONSE_KIND_CRON",
    "E2A_RESPONSE_KIND_E2A_CHUNK",
    "E2A_RESPONSE_KIND_E2A_COMPLETE",
    "E2A_RESPONSE_KIND_E2A_ERROR",
    "E2A_RESPONSE_KIND_PLAN_APPROVAL_REQUIRED",
    "E2A_RESPONSE_STATUS_FAILED",
    "E2A_RESPONSE_STATUS_IN_PROGRESS",
    "E2A_RESPONSE_STATUS_SUCCEEDED",
    "E2A_SOURCE_PROTOCOL_E2A",
    "IdentityOrigin",
    "MAX_LEGACY_AGENT_REQUEST_JSON_BYTES",
    "build_fallback_e2a",
    "channel_context_for_channel_reply",
    "copy_team_delta_identity",
    "e2a_from_agent_fields",
    "e2a_response_from_agent_chunk",
    "e2a_response_from_agent_response",
    "e2a_response_to_agent_chunk",
    "e2a_response_to_agent_response",
    "message_to_e2a",
    "message_to_e2a_or_fallback",
    "message_to_legacy_agent_dict",
    "utc_now_iso",
]
