# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Encode JiuwenSwarm request methods for the shared E2A protocol."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from e2a import gateway_normalize as protocol_normalize
from e2a.models import E2AEnvelope

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
