# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web HTTP 路由：mapped 表。"""

from __future__ import annotations

from gateway.channel_manager.web.http_routes.mapped import (
    MAPPED_ROUTES,
    WebHttpMappedRoute,
    catalog_entries,
    mapped_routes_for_edition,
    validate_mapped_routes,
)

__all__ = [
    "MAPPED_ROUTES",
    "WebHttpMappedRoute",
    "catalog_entries",
    "mapped_routes_for_edition",
    "validate_mapped_routes",
]
