# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer ↔ Gateway 链路探活（经 OA 转发）的共享协议辅助。"""

from __future__ import annotations

import secrets
import time
from typing import Any

from jiuwenclaw.e2a.constants import (
    AGENTSERVER_LINK_HEARTBEAT_CHANNEL,
    AGENTSERVER_LINK_HEARTBEAT_EVENT,
)
from jiuwenclaw.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenclaw.schema.agent import AgentResponseChunk


def build_link_heartbeat_wire(*, sandbox_id: str) -> dict[str, Any]:
    """构造经 OA 转发的标准 E2A 响应线 dict（非 server_push）。"""
    sid = str(sandbox_id or "").strip()
    if not sid:
        raise ValueError("build_link_heartbeat_wire: sandbox_id is required")
    ts = format(int(time.time() * 1000), "x")
    suffix = secrets.token_hex(3)
    request_id = f"link-hb-{ts}_{suffix}"
    now = time.time()
    chunk = AgentResponseChunk(
        request_id=request_id,
        channel_id=AGENTSERVER_LINK_HEARTBEAT_CHANNEL,
        payload={
            "event_type": AGENTSERVER_LINK_HEARTBEAT_EVENT,
            "sandbox_id": sid,
            "ts": now,
        },
        is_complete=True,
    )
    wire = encode_agent_chunk_for_wire(
        chunk,
        response_id=request_id,
        sequence=0,
        is_stream=False,
    )
    wire["session_id"] = AGENTSERVER_LINK_HEARTBEAT_CHANNEL
    return wire


def _extract_payload(wire: dict[str, Any]) -> dict[str, Any] | None:
    payload = wire.get("payload")
    if isinstance(payload, dict):
        return payload
    body = wire.get("body")
    if isinstance(body, dict):
        result = body.get("result")
        if isinstance(result, dict):
            return result
        return body
    return None


def is_link_heartbeat_wire(wire: dict[str, Any]) -> bool:
    """判断 Gateway 入站 E2A 帧是否为链路探活。"""
    channel = str(wire.get("channel") or wire.get("channel_id") or "").strip()
    if channel != AGENTSERVER_LINK_HEARTBEAT_CHANNEL:
        return False
    payload = _extract_payload(wire)
    if payload is None:
        return False
    return str(payload.get("event_type") or "").strip() == AGENTSERVER_LINK_HEARTBEAT_EVENT


def extract_link_heartbeat_payload(wire: dict[str, Any]) -> dict[str, Any]:
    return _extract_payload(wire) or {}


def extract_link_heartbeat_sandbox_id(wire: dict[str, Any]) -> str | None:
    payload = _extract_payload(wire)
    if payload is None:
        return None
    sid = str(payload.get("sandbox_id") or "").strip()
    return sid or None
