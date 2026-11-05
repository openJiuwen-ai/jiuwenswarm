# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.wire_codec``。

本模块仅保留 ``jiuwenswarm.common.e2a.wire_codec`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.wire_codec import (  # noqa: F401
    AgentResponse,
    AgentResponseChunk,
    E2AProvenance,
    E2AResponse,
    E2A_PROTOCOL_VERSION,
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_RESPONSE_STATUS_FAILED,
    E2A_SOURCE_PROTOCOL_E2A,
    E2A_WIRE_LEGACY_AGENT_CHUNK_KEY,
    E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY,
    IdentityOrigin,
    e2a_response_from_agent_chunk,
    e2a_response_from_agent_response,
    e2a_response_to_agent_chunk,
    e2a_response_to_agent_response,
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
    is_e2a_response_wire_dict,
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
    utc_now_iso,
)

__all__ = [
    "AgentResponse",
    "AgentResponseChunk",
    "E2AProvenance",
    "E2AResponse",
    "E2A_PROTOCOL_VERSION",
    "E2A_RESPONSE_KIND_E2A_ERROR",
    "E2A_RESPONSE_STATUS_FAILED",
    "E2A_SOURCE_PROTOCOL_E2A",
    "E2A_WIRE_LEGACY_AGENT_CHUNK_KEY",
    "E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY",
    "IdentityOrigin",
    "e2a_response_from_agent_chunk",
    "e2a_response_from_agent_response",
    "e2a_response_to_agent_chunk",
    "e2a_response_to_agent_response",
    "encode_agent_chunk_for_wire",
    "encode_agent_response_for_wire",
    "encode_json_parse_error_wire",
    "is_e2a_response_wire_dict",
    "parse_agent_server_wire_chunk",
    "parse_agent_server_wire_unary",
    "utc_now_iso",
]
