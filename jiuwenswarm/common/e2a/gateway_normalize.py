# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Protocol aliases with host-owned Team delta identity preservation."""

from typing import Any

from gateway_protocol.e2a import gateway_normalize as protocol_normalize
from gateway_protocol.e2a.agent_models import AgentResponseChunk
from gateway_protocol.e2a.models import E2AResponse
from gateway_protocol.e2a.gateway_normalize import (  # noqa: F401
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
    MAX_LEGACY_AGENT_REQUEST_JSON_BYTES,
    build_fallback_e2a,
    channel_context_for_channel_reply,
    e2a_from_agent_fields,
    e2a_response_from_agent_response,
    e2a_response_to_agent_response,
    message_to_e2a,
    message_to_e2a_or_fallback,
    message_to_legacy_agent_dict,
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
        chunk, response_id=response_id, sequence=sequence,
        is_stream=is_stream, timestamp=timestamp,
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
