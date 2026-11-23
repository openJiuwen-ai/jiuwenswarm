# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``agent_gateway.extensions.sdk.connector_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
对齐设计 D-06 / ``S_01_connector-source-spi``：swarm 侧不得保留第二份 SPI 实现。

SPI v2 符号（``CONNECTOR_CAPABILITIES`` / ``ConnectorDetail`` 等）与本转发模块
同步引入 agent-gateway 7c10913f；38a834a44 升级 skill SPI 时曾同帧升级 pin，
skill 侧转发模块出现过"新符号 + 新 pin"耦合导致的依赖漂移 brick（JW-68）。
为避免同类问题，这里对可能随未来 pin 升级变化的 v2 符号保留 ImportError 降级：
能力集合降为空集；验证错误与验证结果提供 fail-closed 替身（结果构造后恒为
``verified=False``）；``ConnectorDetail`` 等仅由特定调用点使用的符号保持 ``None``，
消费方须判空。任何一种降级都不会让 Worker 与 gateway 双进程启动失败。
"""

from __future__ import annotations

from agent_gateway.extensions.sdk.connector_source import (
    ConnectorArtifactDescriptor,
    ConnectorCandidate,
    ConnectorRef,
    ConnectorSearchRequest,
    ConnectorSearchResult,
    ConnectorSourceConfig,
    ConnectorSourceDescriptor,
    ConnectorSourceExtension,
    ConnectorSourceProvider,
    ConnectorUpdateStatus,
    DownloadPolicy,
    InstalledConnectorArtifact,
    ProviderInvocationContext,
)

try:  # SPI v2 符号随未来 pin 升级演进；旧版 gateway 缺失时使用降级替身。
    from agent_gateway.extensions.sdk.connector_source import (
        CONNECTOR_CAPABILITIES,
        ConnectorArtifactVerificationError,
        ConnectorCapabilityError,
        ConnectorDetail,
        ConnectorDownloadError,
        ConnectorVerificationResult,
    )
except ImportError:  # pragma: no cover - 仅在依赖版本漂移（pin 未同步）时触发
    # 能力集合降为空集，保证调用方的 membership 判断不会因 None 抛错。
    CONNECTOR_CAPABILITIES = frozenset()

    class ConnectorArtifactVerificationError(RuntimeError):  # type: ignore[no-redef]
        """Fail-closed stand-in carrying the SPI code/message contract."""

        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class ConnectorVerificationResult:  # type: ignore[no-redef]
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
    ConnectorCapabilityError = None
    ConnectorDetail = None
    ConnectorDownloadError = None

__all__ = [
    "CONNECTOR_CAPABILITIES",
    "ConnectorArtifactDescriptor",
    "ConnectorArtifactVerificationError",
    "ConnectorCandidate",
    "ConnectorCapabilityError",
    "ConnectorDetail",
    "ConnectorDownloadError",
    "ConnectorRef",
    "ConnectorSearchRequest",
    "ConnectorSearchResult",
    "ConnectorSourceConfig",
    "ConnectorSourceDescriptor",
    "ConnectorSourceExtension",
    "ConnectorSourceProvider",
    "ConnectorUpdateStatus",
    "ConnectorVerificationResult",
    "DownloadPolicy",
    "InstalledConnectorArtifact",
    "ProviderInvocationContext",
]
