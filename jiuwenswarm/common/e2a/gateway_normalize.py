# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.gateway_normalize``。

本模块仅保留 ``jiuwenswarm.common.e2a.gateway_normalize`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.gateway_normalize import (  # noqa: F401
    E2AEnvelope,
    E2AProvenance,
    E2AResponse,
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
    e2a_from_agent_fields,
    e2a_response_from_agent_chunk,
    e2a_response_from_agent_response,
    e2a_response_to_agent_chunk,
    e2a_response_to_agent_response,
    message_to_e2a,
    message_to_e2a_or_fallback,
    message_to_legacy_agent_dict,
    utc_now_iso,
)

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
