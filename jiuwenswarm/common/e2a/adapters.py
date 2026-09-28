# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``gateway_protocol.e2a.adapters``。

注意：``build_acp_tool_response_message`` 增设 ``message_factory=`` 注入
（协议包不依赖本仓 ``Message``）；本仓调用方见 acp_connect.py。
"""

from gateway_protocol.e2a.adapters import (  # noqa: F401
    build_acp_tool_response_message,
    e2a_response_to_a2a_stream_payload,
    e2a_response_to_acp_jsonrpc_response,
    envelope_from_a2a_send_message,
    envelope_from_acp_jsonrpc,
    envelope_to_acp_jsonrpc_call,
)
