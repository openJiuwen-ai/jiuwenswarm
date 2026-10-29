# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.acp.session_updates``。

本模块仅保留 ``jiuwenswarm.common.e2a.acp.session_updates`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.acp.session_updates import (  # noqa: F401
    AcpSessionUpdateState,
    build_acp_final_text_update,
    build_acp_session_update,
    build_acp_todo_update,
    build_acp_tool_call_update,
    build_acp_tool_result_update,
    build_acp_usage_update,
    is_reasoning_event,
)

__all__ = [
    "AcpSessionUpdateState",
    "build_acp_final_text_update",
    "build_acp_session_update",
    "build_acp_todo_update",
    "build_acp_tool_call_update",
    "build_acp_tool_result_update",
    "build_acp_usage_update",
    "is_reasoning_event",
]
