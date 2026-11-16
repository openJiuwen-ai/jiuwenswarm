# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Local request normalization and shared E2A response compatibility helpers.

Request conversion is copied from e2a and uses the local ReqMethod. Response
conversion retains the shared wire codec and Team/organization delta identity.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def method_value(method: ReqMethod | str) -> str:
    """Return a non-empty method name. Callers handle absent methods.

    Raises:
        TypeError: The method is neither a ReqMethod nor a string.
        ValueError: A supplied method name is empty or whitespace-only.
    """
    value = method.value if isinstance(method, ReqMethod) else method
    if not isinstance(value, str):
        raise TypeError("method must be ReqMethod or str")
    if not value.strip():
        raise ValueError("method must be a non-empty string")
    return value


def message_to_legacy_agent_dict(msg: "Message") -> dict[str, Any]:
    """从 Message 生成与历史 WebSocket 一致的 dict（用于兜底 legacy_agent_request）。"""
    rm_val = None if msg.req_method is None else method_value(msg.req_method)
    out: dict[str, Any] = {
        "request_id": msg.id,
        "channel_id": msg.channel_id,
        "session_id": msg.session_id,
        "user_id": msg.user_id,
        "req_method": rm_val,
        "params": dict(msg.params or {}),
        "is_stream": bool(msg.is_stream),
        "timestamp": float(msg.timestamp),
    }
    if msg.metadata:
        out["metadata"] = dict(msg.metadata)
    return out


def message_to_e2a(msg: "Message") -> E2AEnvelope:
    """Message → E2AEnvelope（不经兜底）。"""
    d: dict[str, Any] = {
        "request_id": msg.id,
        "channel_id": msg.channel_id,
        "session_id": msg.session_id,
        "user_id": msg.user_id,
        "chat_id": msg.chat_id,
        "params": dict(msg.params or {}),
        "is_stream": bool(msg.is_stream),
        "timestamp": msg.timestamp,
    }
    if msg.req_method is not None:
        d["method"] = method_value(msg.req_method)
    # 合并 metadata 和独立字段（enable_memory, group_digital_avatar 等）
    metadata: dict[str, Any] = dict(msg.metadata or {})

    # enable_memory 逻辑：只有当 enable_memory=False 且 group_digital_avatar=True 且 is_group_chat=True 时才禁用记忆
    # is_group_chat 通过 metadata 中的 avatar_mode 判断
    is_group_chat = bool(metadata.get("avatar_mode", False))
    should_disable_memory = (
        msg.enable_memory is False  # 配置中明确设置为 false
        and msg.group_digital_avatar is True  # 数字分身模式
        and is_group_chat is True  # 群聊消息
    )
    # 默认启用记忆，只有在上述三个条件同时满足时才禁用
    final_enable_memory = not should_disable_memory
    # 只有当 msg.enable_memory 不为 None 时，才将 enable_memory 写入 metadata
    if msg.enable_memory is not None:
        metadata["enable_memory"] = final_enable_memory
    logger.info(
        "[E2A][enable_memory] msg.enable_memory=%s msg.group_digital_avatar=%s "
        "is_group_chat=%s should_disable=%s final=%s",
        msg.enable_memory, msg.group_digital_avatar, is_group_chat,
        should_disable_memory, final_enable_memory
    )

    if msg.group_digital_avatar:
        metadata["group_digital_avatar"] = msg.group_digital_avatar
    if metadata:
        d["metadata"] = metadata

    # ── 传递 agent_ref / app_id 到 E2A 线协议 ──
    if getattr(msg, "agent_ref", None):
        d["agent_ref"] = msg.agent_ref
    if getattr(msg, "app_id", None):
        d["app_id"] = msg.app_id

    return E2AEnvelope.from_dict(d)


def message_to_e2a_or_fallback(msg: "Message") -> E2AEnvelope:
    """Message → E2A；失败或校验不通过则 build_fallback_e2a。"""
    try:
        env = message_to_e2a(msg)
        if not (env.request_id and str(env.request_id).strip()):
            raise ValueError("empty request_id")
        if env.params is not None and not isinstance(env.params, dict):
            raise ValueError("params must be dict")
        logger.info(
            "[E2A][norm] request_id=%s channel=%s method=%s is_stream=%s params_keys=%s",
            env.request_id,
            env.channel,
            env.method,
            env.is_stream,
            list((env.params or {}).keys())[:32],
        )
        return env
    except Exception as e:
        legacy = message_to_legacy_agent_dict(msg)
        logger.warning(
            "[E2A][fallback] normalize failed request_id=%s err=%s",
            getattr(msg, "id", ""),
            e,
        )
        return build_fallback_e2a(legacy)


def e2a_from_agent_fields(
    *,
    request_id: str,
    channel_id: str = "",
    session_id: str | None = None,
    req_method: "ReqMethod | str | None" = None,
    params: dict[str, Any] | None = None,
    is_stream: bool = False,
    timestamp: float = 0.0,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> E2AEnvelope:
    """由与 AgentRequest 相同的字段构造 E2A（heartbeat / cron / app 管理请求等）。"""
    d: dict[str, Any] = {
        "request_id": request_id,
        "channel_id": channel_id,
        "session_id": session_id,
        "user_id": user_id,
        "params": dict(params or {}),
        "is_stream": is_stream,
        "timestamp": timestamp,
    }
    if req_method is not None:
        d["method"] = method_value(req_method)
    if metadata:
        d["metadata"] = dict(metadata)
    return E2AEnvelope.from_dict(d)


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
    "method_value",
    "utc_now_iso",
]
