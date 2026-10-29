# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名：实现已迁 ``openjiuwen_protocol.e2a.agent_compat``。

E2A → ``AgentRequest`` 的转换规则是两侧共享的线上契约，唯一真源在协议包；
本模块仅保留 ``jiuwenswarm.common.e2a.agent_compat`` 旧 import 路径兼容。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.agent_compat import (  # noqa: F401
    e2a_to_agent_request,
)

__all__ = ["e2a_to_agent_request"]
