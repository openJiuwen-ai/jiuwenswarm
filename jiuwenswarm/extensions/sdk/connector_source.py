# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``agent_gateway.extensions.sdk.connector_source``。

母仓只保留旧 import 路径兼容，两条路径指向同一份类身份；新代码请直接用 gateway 路径。
对齐设计 D-06 / ``S_01_connector-source-spi``：swarm 侧不得保留第二份 SPI 实现。
"""

from __future__ import annotations

from agent_gateway.extensions.sdk.connector_source import (
    CONNECTOR_CAPABILITIES,
    ConnectorArtifactDescriptor,
    ConnectorArtifactVerificationError,
    ConnectorCandidate,
    ConnectorCapabilityError,
    ConnectorDetail,
    ConnectorDownloadError,
    ConnectorRef,
    ConnectorSearchRequest,
    ConnectorSearchResult,
    ConnectorSourceConfig,
    ConnectorSourceDescriptor,
    ConnectorSourceExtension,
    ConnectorSourceProvider,
    ConnectorUpdateStatus,
    ConnectorVerificationResult,
    DownloadPolicy,
    InstalledConnectorArtifact,
    ProviderInvocationContext,
)

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
