# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.acp.protocol``。

本模块仅保留 ``jiuwenswarm.common.e2a.acp.protocol`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。

注意：``build_acp_initialize_result`` 的 agent 版本号改由 ``agent_version=`` 注入（协议包不依赖本仓
``common.version``）；本仓调用方见 runtime/agent_manager.py 与 gateway acp_connect.py。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.acp.protocol import (  # noqa: F401
    build_acp_initialize_result,
    build_acp_prompt_result,
    build_acp_session_list_result,
    build_acp_session_new_result,
)

__all__ = [
    "build_acp_initialize_result",
    "build_acp_prompt_result",
    "build_acp_session_list_result",
    "build_acp_session_new_result",
]
