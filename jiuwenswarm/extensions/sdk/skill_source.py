# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名：实现已迁 ``gateway.extensions.sdk.skill_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
"""

from __future__ import annotations

from agent_gateway.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    ArtifactRef,
    DownloadPolicy,
    InstalledArtifact,
    ProviderInvocationContext,
    SecretResolver,
    SkillCandidate,
    SkillRef,
    SkillSearchRequest,
    SkillSearchResult,
    SkillSourceExtension,
    SkillSourceProvider,
    SkillUpdateStatus,
    SourceConfig,
    SourceDescriptor,
    TrustPolicy,
)

__all__ = [
    "ArtifactDescriptor",
    "ArtifactRef",
    "DownloadPolicy",
    "InstalledArtifact",
    "ProviderInvocationContext",
    "SecretResolver",
    "SkillCandidate",
    "SkillRef",
    "SkillSearchRequest",
    "SkillSearchResult",
    "SkillSourceExtension",
    "SkillSourceProvider",
    "SkillUpdateStatus",
    "SourceConfig",
    "SourceDescriptor",
    "TrustPolicy",
]
