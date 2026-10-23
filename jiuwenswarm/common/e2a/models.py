# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名（过渡形态）：实现已迁 ``openjiuwen_protocol.e2a.models``。

本模块仅保留 ``jiuwenswarm.common.e2a.models`` 旧 import 路径兼容，re-export openjiuwen-protocol
包的同一对象（含原模块从兄弟模块引入的符号，保持命名空间一致）；
唯一真源在协议包，本仓不持有实现。过渡期结束后随 common 副本收敛一并删除。
"""

from __future__ import annotations

from openjiuwen_protocol.e2a.models import (  # noqa: F401
    E2AAuth,
    E2AEnvelope,
    E2AFileRef,
    E2AProvenance,
    E2AResponse,
    E2A_PROTOCOL_VERSION,
    E2A_RESPONSE_STATUS_IN_PROGRESS,
    E2A_SOURCE_PROTOCOL_A2A,
    E2A_SOURCE_PROTOCOL_ACP,
    E2A_SOURCE_PROTOCOL_E2A,
    E2A_WIRE_INTERNAL_METADATA_KEYS,
    IdentityOrigin,
    merge_params_to_acp_prompt,
    utc_now_iso,
)

__all__ = [
    "E2AAuth",
    "E2AEnvelope",
    "E2AFileRef",
    "E2AProvenance",
    "E2AResponse",
    "E2A_PROTOCOL_VERSION",
    "E2A_RESPONSE_STATUS_IN_PROGRESS",
    "E2A_SOURCE_PROTOCOL_A2A",
    "E2A_SOURCE_PROTOCOL_ACP",
    "E2A_SOURCE_PROTOCOL_E2A",
    "E2A_WIRE_INTERNAL_METADATA_KEYS",
    "IdentityOrigin",
    "merge_params_to_acp_prompt",
    "utc_now_iso",
]
