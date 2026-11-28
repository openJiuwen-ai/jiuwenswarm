# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""转发别名：实现已迁 ``agent_gateway.extensions.sdk.skill_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
Hub 私有 Trust / HMAC 见 ``jiuwenswarm.extensions.swarm_skill_hub.hub_verify``；
通用 download HMAC 见 ``jiuwenswarm.server.runtime.skill.artifact_security``。

SPI v2 新增符号（``SKILL_CAPABILITIES`` / ``SkillDetail`` / ``SkillDownloadError`` /
``SkillCapabilityError`` / ``SkillArtifactVerificationError`` / ``SkillVerificationResult``，
connector 侧同构）随 agent-gateway pin 升级引入；pin 与代码升级不同帧（依赖版本漂移）
时这些符号缺失。本模块处于 Worker 与 gateway 双进程的启动必经路径，模块级硬失败会把
一次依赖漂移放大为双进程不可用，因此缺失时降级而不是抛 ``ImportError``：能力集合降为
空集；验证错误与验证结果提供 fail-closed 替身（结果构造后恒为 ``verified=False``，
安装审计必然失败）；仅供特定调用点的可选符号保持 ``None``。依赖缺失场景技能市场相关
能力不可用，其余能力不受影响。
"""

from __future__ import annotations

import logging

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

try:  # SPI v2 符号（agent-gateway dda0e957 起）；旧版 gateway 缺失时使用降级替身。
    from agent_gateway.extensions.sdk.skill_source import (
        SKILL_CAPABILITIES,
        SkillArtifactVerificationError,
        SkillCapabilityError,
        SkillDetail,
        SkillDownloadError,
        SkillVerificationResult,
    )
except ImportError as exc:  # pragma: no cover - 仅在依赖版本漂移（pin 未同步）时触发
    # 降级必须可见：静默降级会把安装审计失败（artifact_signature_invalid）等表象
    # 引向签名/供应链方向，掩盖真实根因（agent-gateway pin 未同步）。
    logging.getLogger(__name__).warning(
        "agent-gateway is missing SPI v2 skill symbols (%s); degrading: "
        "SKILL_CAPABILITIES degraded to empty set, verification results are "
        "fail-closed (verified=False), optional symbols set to None. "
        "Sync the agent-gateway pin to restore.",
        exc,
    )
    # 能力集合降为空集，保证调用方的 membership 判断不会因 None 抛错。
    SKILL_CAPABILITIES = frozenset()

    class SkillArtifactVerificationError(RuntimeError):  # type: ignore[no-redef]
        """Fail-closed stand-in carrying the SPI code/message contract."""

        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class SkillVerificationResult:  # type: ignore[no-redef]
        """Fail-closed stand-in for the SPI v2 verification result.

        Accepts the same constructor arguments as the gateway dataclass but
        always reports ``verified=False``, so a process running against an
        older gateway can never complete an install-path audit.
        """

        def __init__(
            self,
            verified: bool = False,
            content_sha256: str = "",
            algorithm: str | None = None,
            scope: str | None = None,
            key_id: str | None = None,
            verified_at: str | None = None,
            metadata: dict | None = None,
        ) -> None:
            self.verified = False
            self.content_sha256 = content_sha256
            self.algorithm = algorithm
            self.scope = scope
            self.key_id = key_id
            self.verified_at = verified_at
            self.metadata = dict(metadata or {})

    # Symbols only referenced by specific call sites stay absent.
    SkillCapabilityError = None
    SkillDetail = None
    SkillDownloadError = None

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
