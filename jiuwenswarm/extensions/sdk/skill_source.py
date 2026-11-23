# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名：实现已迁 ``agent_gateway.extensions.sdk.skill_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
Hub 私有 Trust / HMAC 见 ``jiuwenswarm.extensions.swarm_skill_hub.hub_verify``；
通用 download HMAC 见 ``jiuwenswarm.server.runtime.skill.artifact_security``。

SPI v2 新增符号（``SKILL_CAPABILITIES`` / ``SkillDetail`` / ``SkillDownloadError`` /
``SkillCapabilityError`` / ``SkillArtifactVerificationError`` / ``SkillVerificationResult``，
connector 侧同构）随 agent-gateway pin 升级引入；pin 与代码升级不同帧（依赖版本漂移）
时这些符号缺失，转发模块按 ``None`` 降级而不是抛 ``ImportError``——本模块处于 Worker
与 gateway 双进程的启动必经路径，模块级硬失败会把一次依赖漂移放大为双进程不可用。
依赖缺失场景技能市场相关能力不可用（消费方须对 ``None`` 判空），其余能力不受影响。
"""

from __future__ import annotations

from agent_gateway.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    DownloadPolicy,
    InstalledArtifact,
    ProviderInvocationContext,
    SkillCandidate,
    SkillRef,
    SkillSearchRequest,
    SkillSearchResult,
    SkillSourceExtension,
    SkillSourceProvider,
    SkillUpdateStatus,
    SourceConfig,
    SourceDescriptor,
)

try:  # SPI v2 符号（agent-gateway dda0e957 起）；旧版 gateway 缺失时降级为 None。
    from agent_gateway.extensions.sdk.skill_source import (
        SKILL_CAPABILITIES,
        SkillArtifactVerificationError,
        SkillCapabilityError,
        SkillDetail,
        SkillDownloadError,
        SkillVerificationResult,
    )
except ImportError:  # pragma: no cover - 仅在依赖版本漂移（pin 未同步）时触发
    SKILL_CAPABILITIES = None
    SkillArtifactVerificationError = None
    SkillCapabilityError = None
    SkillDetail = None
    SkillDownloadError = None
    SkillVerificationResult = None

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
