# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bounded WebSocket wire sending for AgentServer responses."""

from __future__ import annotations

import json
import logging
from typing import Any

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES

logger = logging.getLogger(__name__)

_ROUTING_KEYS = (
    "session_id",
    "task_id",
    "context_id",
    "correlation_id",
)


def _oversized_payload(actual_bytes: int) -> dict[str, Any]:
    return {
        "error": "AgentServer response exceeds WebSocket send budget",
        "code": "response_too_large",
        "actual_bytes": actual_bytes,
        "max_bytes": AGENT_WS_SEND_BUDGET_BYTES,
    }


def _build_oversized_fallback(
    wire: dict[str, Any], actual_bytes: int
) -> dict[str, Any]:
    request_id = str(wire.get("request_id") or "")
    response_id = str(wire.get("response_id") or request_id)
    channel_id = str(wire.get("channel") or "")
    sequence = int(wire.get("sequence") or 0)
    agent_ref = wire.get("agent_ref")
    payload = _oversized_payload(actual_bytes)

    if wire.get("is_stream"):
        payload["event_type"] = "chat.error"
        fallback = encode_agent_chunk_for_wire(
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload=payload,
                is_complete=True,
                agent_ref=agent_ref,
            ),
            response_id=response_id,
            sequence=sequence,
        )
    elif wire.get("type") == "event":
        fallback = {
            "type": "event",
            "event": "response.error",
            "payload": payload,
        }
    else:
        fallback = encode_agent_response_for_wire(
            AgentResponse(
                request_id=request_id,
                channel_id=channel_id,
                ok=False,
                payload=payload,
                agent_ref=agent_ref,
            ),
            response_id=response_id,
            sequence=sequence,
        )

    for key in _ROUTING_KEYS:
        if wire.get(key) is not None:
            fallback[key] = wire[key]

    source_metadata = wire.get("metadata")
    if (
        isinstance(source_metadata, dict)
        and source_metadata.get(E2A_WIRE_SERVER_PUSH_KEY) is True
    ):
        metadata = dict(fallback.get("metadata") or {})
        metadata[E2A_WIRE_SERVER_PUSH_KEY] = True
        fallback["metadata"] = metadata

    return fallback


async def send_wire_payload(ws: Any, wire: dict[str, Any]) -> bool:
    """Send one bounded wire payload, replacing oversized data with an error."""
    serialized = json.dumps(wire, ensure_ascii=False)
    actual_bytes = len(serialized.encode("utf-8"))
    if actual_bytes <= AGENT_WS_SEND_BUDGET_BYTES:
        await ws.send(serialized)
        return True

    _preview = serialized[:1000]
    if len(serialized) > 1000:
        _preview += "...(truncated)"
    logger.error(
        "AgentServer WebSocket response too large: "
        "request_id=%s session_id=%s channel=%s type=%s is_stream=%s "
        "response_kind=%s actual_bytes=%d max_bytes=%d preview=%s",
        wire.get("request_id"),
        wire.get("session_id"),
        wire.get("channel") or wire.get("channel_id"),
        wire.get("type"),
        wire.get("is_stream"),
        wire.get("response_kind"),
        actual_bytes,
        AGENT_WS_SEND_BUDGET_BYTES,
        _preview,
    )
    fallback = _build_oversized_fallback(wire, actual_bytes)
    fallback_json = json.dumps(fallback, ensure_ascii=False)
    fallback_bytes = len(fallback_json.encode("utf-8"))
    if fallback_bytes > AGENT_WS_SEND_BUDGET_BYTES:
        raise RuntimeError(
            "oversized fallback exceeds WebSocket send budget: "
            f"actual_bytes={fallback_bytes} "
            f"max_bytes={AGENT_WS_SEND_BUDGET_BYTES}"
        )
    await ws.send(fallback_json)
    return False
