# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名：实现已迁 ``gateway.extensions.sdk.artifact_security``。

母仓只保留旧 import 路径兼容，两条路径指向同一份实现；新代码请直接用 gateway 路径。
"""

from __future__ import annotations

from agent_gateway.extensions.sdk.artifact_security import (
    ArtifactVerificationError,
    EnvironmentSecretResolver,
    MappingSecretResolver,
    VerificationResult,
    verify_skill_download_hmac,
    verify_skillhub_artifact,
)

__all__ = [
    "ArtifactVerificationError",
    "EnvironmentSecretResolver",
    "MappingSecretResolver",
    "VerificationResult",
    "verify_skill_download_hmac",
    "verify_skillhub_artifact",
]
