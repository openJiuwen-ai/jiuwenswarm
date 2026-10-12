# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Protocol wire aliases with host-owned Team delta identity preservation."""

from typing import Any

from gateway_protocol.e2a import wire_codec as protocol_wire
from gateway_protocol.e2a.agent_models import AgentResponseChunk
from gateway_protocol.e2a.wire_codec import (  # noqa: F401
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
    is_e2a_response_wire_dict,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.e2a.gateway_normalize import (  # noqa: F401
    copy_team_delta_identity,
    e2a_response_from_agent_chunk,
)


def encode_agent_chunk_for_wire(
    chunk: AgentResponseChunk,
    *,
    response_id: str,
    sequence: int,
    is_stream: bool = True,
) -> dict[str, Any]:
    """Keep protocol encoding and add only the host Team delta identity."""
    wire = protocol_wire.encode_agent_chunk_for_wire(
        chunk, response_id=response_id, sequence=sequence, is_stream=is_stream,
    )
    if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.delta":
        copy_team_delta_identity(chunk.payload, wire["body"])
    return wire


def parse_agent_server_wire_chunk(data: dict[str, Any]) -> AgentResponseChunk:
    """Keep protocol parsing and restore only the host Team delta identity."""
    chunk = protocol_wire.parse_agent_server_wire_chunk(data)
    if (
        is_e2a_response_wire_dict(data)
        and isinstance(chunk.payload, dict)
        and chunk.payload.get("event_type") == "chat.delta"
    ):
        copy_team_delta_identity(data["body"], chunk.payload)
    return chunk
