# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""E2A server_push 线编码：WebSocket 与 HTTP SSE 下行共用同一 wire 形状。"""

from __future__ import annotations

from typing import Any

from jiuwenclaw.e2a.constants import (
    E2A_RESPONSE_STATUS_SUCCEEDED,
    E2A_WIRE_INTERNAL_METADATA_KEYS,
    E2A_WIRE_SERVER_PUSH_KEY,
)
from jiuwenclaw.e2a.models import E2AProvenance, E2AResponse, IdentityOrigin, utc_now_iso
from jiuwenclaw.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenclaw.schema.agent import AgentResponseChunk

_CONVERTER = "jiuwenclaw.agentserver.gateway_push.wire:build_server_push_wire"


def build_server_push_wire(msg: dict[str, Any]) -> dict[str, Any]:
    """将 send_push 入参编码为与 WebSocket 单帧一致的 E2A 响应线 dict。"""
    response_kind = str(msg.get("response_kind") or "").strip()
    if response_kind:
        wire = E2AResponse(
            response_id=str(msg.get("request_id", "")),
            request_id=str(msg.get("request_id", "")),
            sequence=0,
            is_final=True,
            status=E2A_RESPONSE_STATUS_SUCCEEDED,
            response_kind=response_kind,
            timestamp=utc_now_iso(),
            provenance=E2AProvenance(
                source_protocol="e2a",
                converter=_CONVERTER,
                converted_at=utc_now_iso(),
                details={"kind": "server_push"},
            ),
            body=dict(msg.get("body") or {}),
            channel=str(msg.get("channel_id", "")) or None,
            session_id=msg.get("session_id"),
            identity_origin=IdentityOrigin.AGENT,
            is_stream=False,
            metadata=dict(msg.get("metadata") or {}),
        ).to_dict()
        md = dict(wire.get("metadata") or {})
        md[E2A_WIRE_SERVER_PUSH_KEY] = True
        wire["metadata"] = md
        return wire

    chunk = AgentResponseChunk(
        request_id=str(msg.get("request_id", "")),
        channel_id=str(msg.get("channel_id", "")),
        payload=msg.get("payload"),
        is_complete=bool(msg.get("is_complete", False)),
    )
    wire = encode_agent_chunk_for_wire(
        chunk,
        response_id=str(msg.get("request_id", "")),
        sequence=0,
    )
    md = dict(wire.get("metadata") or {})
    um = msg.get("metadata")
    if isinstance(um, dict):
        for k, v in um.items():
            if k in E2A_WIRE_INTERNAL_METADATA_KEYS:
                continue
            md[k] = v
    md[E2A_WIRE_SERVER_PUSH_KEY] = True
    wire["metadata"] = md
    sid = msg.get("session_id")
    if sid is not None and str(sid).strip():
        wire["session_id"] = str(sid)
    return wire
