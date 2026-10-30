# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""E2A（Everything-to-Agent）：统一信封；ACP / A2A 等经转换进入 E2A，并由 provenance 记录出处。

**转发别名（过渡形态）**：E2A wire 契约（常量/模型/规范化/适配/线编解码/ACP 辅助）
已迁 ``openjiuwen_protocol.e2a``；本包 re-export 同一对象，仓内既有
``from jiuwenswarm.common.e2a import ...`` 与 ``from jiuwenswarm.common.e2a.acp import ...``
全部保持可用。唯一真源在协议包，本仓不持有实现，过渡期结束后随收敛删除。

**未随迁（本仓保留实现）**：
- ``agent_compat.e2a_to_agent_request``：依赖本仓 ``schema.agent.AgentRequest``
  与 ``schema.message.ReqMethod`` 实例构造（ReqMethod 耦合 Mode 产品逻辑，
  尚未协议化），见 ``jiuwenswarm/common/e2a/agent_compat.py``。
- ``gateway_normalize`` 中 Team/组织 chat.delta 身份字段包装（enterprise）。

协议说明（中英）：仓库 ``docs/zh/E2A-protocol.md``、``docs/en/E2A-protocol.md``。
"""

from openjiuwen_protocol.e2a.adapters import (
    e2a_response_to_a2a_stream_payload,
    e2a_response_to_acp_jsonrpc_response,
    envelope_from_a2a_send_message,
    envelope_from_acp_jsonrpc,
    envelope_to_acp_jsonrpc_call,
)
from openjiuwen_protocol.e2a.acp.protocol import (
    build_acp_initialize_result,
    build_acp_prompt_result,
)
from openjiuwen_protocol.e2a.acp.session_updates import (
    AcpSessionUpdateState,
    build_acp_final_text_update,
    build_acp_session_update,
    build_acp_usage_update,
)
from openjiuwen_protocol.e2a.agent_models import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)
from openjiuwen_protocol.e2a.constants import (
    ACP_AGENT_TO_CLIENT_METHODS,
    ACP_CLIENT_TO_AGENT_METHODS,
    ACP_NOTIFICATION_NAMES,
    ACP_SESSION_UPDATE_KINDS,
    E2A_A2A_STREAM_BRANCHES,
    E2A_RESPONSE_KINDS,
    E2A_WIRE_LEGACY_AGENT_CHUNK_KEY,
    E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY,
    E2A_WIRE_SERVER_PUSH_KEY,
    E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR,
    E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
    E2A_RESPONSE_KIND_ACP_SESSION_UPDATE,
    E2A_RESPONSE_KIND_A2A_MESSAGE,
    E2A_RESPONSE_KIND_A2A_STREAM_EVENT,
    E2A_RESPONSE_KIND_A2A_TASK,
    E2A_RESPONSE_KIND_E2A_CHUNK,
    E2A_RESPONSE_KIND_E2A_COMPLETE,
    E2A_RESPONSE_KIND_E2A_ERROR,
    E2A_RESPONSE_KIND_EXT,
    E2A_RESPONSE_STATUS_FAILED,
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_RESPONSE_STATUS_SUCCEEDED,
    E2A_SOURCE_PROTOCOL_A2A,
    E2A_SOURCE_PROTOCOL_ACP,
    E2A_SOURCE_PROTOCOL_E2A,
)
from openjiuwen_protocol.e2a.gateway_normalize import (
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
from openjiuwen_protocol.e2a.wire_codec import (
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
    is_e2a_response_wire_dict,
    parse_agent_server_wire_unary,
)
from openjiuwen_protocol.e2a.models import (
    E2A_PROTOCOL_VERSION,
    E2AAuth,
    E2AEnvelope,
    E2AFileRef,
    E2AProvenance,
    E2AResponse,
    IdentityOrigin,
    merge_params_to_acp_prompt,
    utc_now_iso,
)

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.gateway_normalize import (
    e2a_response_from_agent_chunk,
    e2a_response_to_agent_chunk,
)
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    parse_agent_server_wire_chunk,
)

__all__ = [
    "ACP_AGENT_TO_CLIENT_METHODS",
    "ACP_CLIENT_TO_AGENT_METHODS",
    "ACP_NOTIFICATION_NAMES",
    "ACP_SESSION_UPDATE_KINDS",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "AcpSessionUpdateState",
    "E2A_A2A_STREAM_BRANCHES",
    "E2A_PROTOCOL_VERSION",
    "E2A_RESPONSE_KINDS",
    "E2A_RESPONSE_KIND_ACP_JSONRPC_ERROR",
    "E2A_RESPONSE_KIND_ACP_PROMPT_RESULT",
    "E2A_RESPONSE_KIND_ACP_SESSION_UPDATE",
    "E2A_RESPONSE_KIND_A2A_MESSAGE",
    "E2A_RESPONSE_KIND_A2A_STREAM_EVENT",
    "E2A_RESPONSE_KIND_A2A_TASK",
    "E2A_RESPONSE_KIND_E2A_CHUNK",
    "E2A_RESPONSE_KIND_E2A_COMPLETE",
    "E2A_RESPONSE_KIND_E2A_ERROR",
    "E2A_RESPONSE_KIND_EXT",
    "E2A_WIRE_LEGACY_AGENT_CHUNK_KEY",
    "E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY",
    "E2A_WIRE_SERVER_PUSH_KEY",
    "E2A_RESPONSE_STATUS_FAILED",
    "E2A_RESPONSE_STATUS_IN_PROGRESS",
    "E2A_RESPONSE_STATUS_SUCCEEDED",
    "E2A_SOURCE_PROTOCOL_A2A",
    "E2A_SOURCE_PROTOCOL_ACP",
    "E2A_SOURCE_PROTOCOL_E2A",
    "E2AAuth",
    "E2AEnvelope",
    "E2AFileRef",
    "E2AProvenance",
    "E2AResponse",
    "IdentityOrigin",
    "PermissionContext",
    "e2a_response_to_a2a_stream_payload",
    "e2a_response_to_acp_jsonrpc_response",
    "envelope_from_a2a_send_message",
    "envelope_from_acp_jsonrpc",
    "envelope_to_acp_jsonrpc_call",
    "merge_params_to_acp_prompt",
    "utc_now_iso",
    "e2a_to_agent_request",
    "build_acp_final_text_update",
    "build_acp_initialize_result",
    "build_acp_prompt_result",
    "build_acp_session_update",
    "build_acp_usage_update",
    "E2A_FALLBACK_FAILED_KEY",
    "E2A_INTERNAL_CONTEXT_KEY",
    "E2A_LEGACY_AGENT_REQUEST_KEY",
    "MAX_LEGACY_AGENT_REQUEST_JSON_BYTES",
    "build_fallback_e2a",
    "channel_context_for_channel_reply",
    "e2a_from_agent_fields",
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
    "message_to_e2a",
    "message_to_e2a_or_fallback",
    "message_to_legacy_agent_dict",
]
