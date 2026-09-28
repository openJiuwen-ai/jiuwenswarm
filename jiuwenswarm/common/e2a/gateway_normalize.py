# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：实现已迁 ``gateway_protocol.e2a.gateway_normalize``。"""

from gateway_protocol.e2a.gateway_normalize import (  # noqa: F401
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
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
)
