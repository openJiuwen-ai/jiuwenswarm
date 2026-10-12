# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""
转发别名（过渡形态）：实现已迁 ``gateway_protocol.e2a.acp.protocol``。

注意：``build_acp_initialize_result`` 的 agent 版本号改由 ``agent_version=``
注入（协议包不依赖本仓 ``common.version``）；本仓调用方见
runtime/agent_manager.py 与 gateway acp_connect.py。
"""

from gateway_protocol.e2a.acp.protocol import (  # noqa: F401
    build_acp_initialize_result,
    build_acp_prompt_result,
    build_acp_session_list_result,
    build_acp_session_new_result,
)
