# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web HTTP 路由：mapped 表 + 特殊挂载。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from jiuwenswarm.gateway.channel_manager.web.http_routes.file_compat import (
    catalog_file_compat_entries,
    register_file_compat_routes,
)
from jiuwenswarm.gateway.channel_manager.web.http_routes.mapped import (
    MAPPED_ROUTES,
    WebHttpMappedRoute,
    catalog_entries,
    mapped_routes_for_edition,
    validate_mapped_routes,
)
from jiuwenswarm.gateway.channel_manager.web.http_routes.sessions_compat import (
    catalog_sessions_compat_entries,
    register_sessions_compat_routes,
)
from jiuwenswarm.gateway.channel_manager.web.http_routes.workspace import (
    register_workspace_http_routes,
)

__all__ = [
    "MAPPED_ROUTES", "WebHttpMappedRoute", "catalog_entries",
    "catalog_file_compat_entries", "catalog_sessions_compat_entries",
    "mapped_routes_for_edition", "register_file_compat_routes",
    "register_sessions_compat_routes", "register_special_http_routes",
    "register_workspace_http_routes", "validate_mapped_routes",
]


def register_special_http_routes(app: FastAPI, channel: Any) -> None:
    register_workspace_http_routes(app, channel)
    register_sessions_compat_routes(app)
    register_file_compat_routes(app)
