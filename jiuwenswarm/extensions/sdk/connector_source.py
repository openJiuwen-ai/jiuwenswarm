# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``agent_gateway.extensions.sdk.connector_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
对齐设计 D-06 / ``S_01_connector-source-spi``：swarm 侧不得保留第二份 SPI 实现。

SPI v2 符号（``CONNECTOR_CAPABILITIES`` / ``ConnectorDetail`` 等）与本转发模块
同步引入 agent-gateway 7c10913f；38a834a44 升级 skill SPI 时曾同帧升级 pin，
skill 侧转发模块出现过"新符号 + 新 pin"耦合导致的依赖漂移 brick（JW-68）。
为避免同类问题，这里对可能随未来 pin 升级变化的 v2 符号保留 ImportError 降级：
缺失时按 ``None`` 兜底而不是让 Worker 与 gateway 双进程启动失败。
消费方（swarm_connector_hub）须对 ``None`` 判空降级，其余能力不受影响。
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

try:  # SPI v2 符号随未来 pin 升级演进；旧版 gateway 缺失时降级为 None。
    from agent_gateway.extensions.sdk.connector_source import (
        CONNECTOR_CAPABILITIES,
        ConnectorArtifactVerificationError,
        ConnectorCapabilityError,
        ConnectorDetail,
        ConnectorDownloadError,
        ConnectorVerificationResult,
    )
except ImportError:  # pragma: no cover - 仅在依赖版本漂移（pin 未同步）时触发
    CONNECTOR_CAPABILITIES = None
    ConnectorArtifactVerificationError = None
    ConnectorCapabilityError = None
    ConnectorDetail = None
    ConnectorDownloadError = None
    ConnectorVerificationResult = None

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
