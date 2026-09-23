# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front-safe E2A parse/encode helpers. No OpenJiuwen or Agent Runtime imports."""

from __future__ import annotations

import json
import logging
from typing import Any

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.constants import E2A_WIRE_INTERNAL_METADATA_KEYS
from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)


def payload_to_request(data: dict[str, Any]) -> AgentRequest:
    """Parse a legacy JSON payload into ``AgentRequest``."""
    req_method = data.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in E2A_WIRE_INTERNAL_METADATA_KEYS
        } or None
    app_id = data.get("app_id")
    if app_id:
        if metadata is None:
            metadata = {}
        metadata.setdefault("app_id", app_id)
    return AgentRequest(
        request_id=data["request_id"],
        channel_id=data.get("channel_id", "web"),
        session_id=data.get("session_id"),
        req_method=req_method,
        params=data.get("params", {}),
        is_stream=data.get("is_stream", False),
        timestamp=data.get("timestamp", 0.0),
        metadata=metadata,
        user_id=str(data.get("user_id") or "").strip(),
    )


def parse_raw_message(raw: str | bytes) -> AgentRequest | dict[str, Any]:
    """Parse one WebSocket text frame.

    Returns an ``AgentRequest`` on success, or a JSON-parse error wire dict.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return encode_json_parse_error_wire(
            request_id="",
            channel_id="",
            message=f"JSON 解析失败: {exc}",
        )
    try:
        env = E2AEnvelope.from_dict(data)
    except Exception as parse_err:
        logger.warning("[Front] E2A from_dict 失败，按旧载荷解析: %s", parse_err)
        return payload_to_request(data)
    jw = (env.channel_context or {}).get(E2A_INTERNAL_CONTEXT_KEY)
    if isinstance(jw, dict) and jw.get(E2A_FALLBACK_FAILED_KEY):
        legacy = jw.get(E2A_LEGACY_AGENT_REQUEST_KEY)
        logger.warning(
            "[E2A][fallback] using legacy_agent_request request_id=%s",
            env.request_id,
        )
        if not isinstance(legacy, dict):
            raise ValueError("legacy_agent_request missing or not a dict")
        return payload_to_request(legacy)
    logger.info(
        "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
        env.request_id,
        env.channel,
        env.method,
        env.is_stream,
    )
    return e2a_to_agent_request(env)


def encode_response(response: AgentResponse, *, response_id: str) -> dict[str, Any]:
    return encode_agent_response_for_wire(response, response_id=response_id)


def encode_chunk(
    chunk: AgentResponseChunk,
    *,
    response_id: str,
    sequence: int,
) -> dict[str, Any]:
    return encode_agent_chunk_for_wire(
        chunk, response_id=response_id, sequence=sequence
    )


def runtime_warming_frame(
    request: AgentRequest,
    *,
    readiness_state: str,
    sequence: int = 0,
) -> dict[str, Any]:
    """Status-only warming event. Never contains assistant business text."""
    chunk = AgentResponseChunk(
        request_id=request.request_id,
        channel_id=request.channel_id,
        payload={
            "event_type": "runtime.warming",
            "readiness": readiness_state,
            "session_id": request.session_id or "",
        },
        is_complete=False,
    )
    return encode_chunk(chunk, response_id=request.request_id, sequence=sequence)


def connection_ack_frame(
    *,
    readiness_state: str,
    heartbeat_job_ready: bool = False,
    heartbeat_protocol_version: int = 1,
) -> dict[str, Any]:
    """First-frame ack: transport connected, plus explicit runtime readiness.

    ``status`` describes the Front connection only. Gateway must read
    ``readiness`` for control/execution availability. ``FAILED`` / ``DRAINING``
    never report business ``status=ready``.
    """
    if readiness_state == "FAILED":
        status = "failed"
    elif readiness_state == "DRAINING":
        status = "draining"
    else:
        status = "ready"
    return {
        "type": "event",
        "event": "connection.ack",
        "payload": {
            "status": status,
            "readiness": readiness_state,
            "heartbeat_job_owner": "agentserver",
            "heartbeat_job_protocol": heartbeat_protocol_version,
            "heartbeat_job_ready": heartbeat_job_ready,
        },
    }
