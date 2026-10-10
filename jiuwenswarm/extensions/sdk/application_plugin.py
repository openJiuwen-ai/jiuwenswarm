# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：应用插件契约已迁 ``gateway_protocol.sdk.application_plugin``。"""

from __future__ import annotations

from gateway_protocol.sdk.application_plugin import (  # noqa: F401
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
    ManifestApplicationPlugin,
    WebSocketEndpoint,
    WebSocketRouteContribution,
)

__all__ = [
    "ApplicationPluginExtension",
    "ApplicationPluginServices",
    "FrontendContribution",
    "ManifestApplicationPlugin",
    "WebSocketEndpoint",
    "WebSocketRouteContribution",
]
