# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.adapters``。

本模块仅保留 ``jiuwenswarm.common.e2a.adapters`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.adapters import (  # noqa: F401
    E2AEnvelope,
    E2AProvenance,
    E2AResponse,
    E2A_A2A_STREAM_BRANCHES,
    E2A_RESPONSE_KIND_A2A_STREAM_EVENT,
    E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
    E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_SOURCE_PROTOCOL_A2A,
    E2A_SOURCE_PROTOCOL_ACP,
    IdentityOrigin,
    build_acp_tool_response_message,
    e2a_response_to_a2a_stream_payload,
    e2a_response_to_acp_jsonrpc_response,
    envelope_from_a2a_send_message,
    envelope_from_acp_jsonrpc,
    envelope_to_acp_jsonrpc_call,
    merge_params_to_acp_prompt,
    utc_now_iso,
)

__all__ = [
    "E2AEnvelope",
    "E2AProvenance",
    "E2AResponse",
    "E2A_A2A_STREAM_BRANCHES",
    "E2A_RESPONSE_KIND_A2A_STREAM_EVENT",
    "E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR",
    "E2A_RESPONSE_KIND_ACP_PROMPT_RESULT",
    "E2A_RESPONSE_KIND_E2A_ERROR",
    "E2A_SOURCE_PROTOCOL_A2A",
    "E2A_SOURCE_PROTOCOL_ACP",
    "IdentityOrigin",
    "build_acp_tool_response_message",
    "e2a_response_to_a2a_stream_payload",
    "e2a_response_to_acp_jsonrpc_response",
    "envelope_from_a2a_send_message",
    "envelope_from_acp_jsonrpc",
    "envelope_to_acp_jsonrpc_call",
    "merge_params_to_acp_prompt",
    "utc_now_iso",
]
