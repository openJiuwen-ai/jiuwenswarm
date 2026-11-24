# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名：实现已迁 ``agent_gateway.extensions.sdk.skill_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
Hub 私有 Trust / HMAC 见 ``jiuwenswarm.extensions.swarm_skill_hub.hub_verify``；
通用 download HMAC 见 ``jiuwenswarm.server.runtime.skill.artifact_security``。
"""

from __future__ import annotations

from agent_gateway.extensions.sdk.skill_source import (
    SKILL_CAPABILITIES,
    ArtifactDescriptor,
    DownloadPolicy,
    InstalledArtifact,
    ProviderInvocationContext,
    SkillArtifactVerificationError,
    SkillCandidate,
    SkillCapabilityError,
    SkillDetail,
    SkillDownloadError,
    SkillRef,
    SkillSearchRequest,
    SkillSearchResult,
    SkillSourceExtension,
    SkillSourceProvider,
    SkillUpdateStatus,
    SkillVerificationResult,
    SourceConfig,
    SourceDescriptor,
)

__all__ = [
    "SKILL_CAPABILITIES",
    "ArtifactDescriptor",
    "DownloadPolicy",
    "InstalledArtifact",
    "ProviderInvocationContext",
    "SkillArtifactVerificationError",
    "SkillCandidate",
    "SkillCapabilityError",
    "SkillDetail",
    "SkillDownloadError",
    "SkillRef",
    "SkillSearchRequest",
    "SkillSearchResult",
    "SkillSourceExtension",
    "SkillSourceProvider",
    "SkillUpdateStatus",
    "SkillVerificationResult",
    "SourceConfig",
    "SourceDescriptor",
]
