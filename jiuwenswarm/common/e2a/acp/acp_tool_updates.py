# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：实现已迁 ``gateway_protocol.e2a.acp.acp_tool_updates``。"""

from gateway_protocol.e2a.acp.acp_tool_updates import (  # noqa: F401
    build_acp_todo_update,
    build_acp_tool_call_update,
    build_acp_tool_descriptor,
    build_acp_tool_result_update,
    is_reasoning_event,
    normalize_tool_name,
)
