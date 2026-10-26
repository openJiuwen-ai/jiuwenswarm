# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register Manager config APIs onto the Gateway unified FastAPI app (WEB_PORT)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from jiuwenswarm.common.security.link_mtls import (
    LinkMTLSConfig,
    LinkMTLSError,
)
from jiuwenswarm.edition import is_enterprise

from ..routers.application_config_routers import application_config_router
from ..routers.instance_resource_routers import instance_resource_router
from ..routers.instance_routers import instance_router
from ..routers.quota_routers import quota_router
from ..routers.template_routers import templates_router

logger = logging.getLogger(__name__)

# Log only fixed reasons, never arbitrary exception text or request data.
_SAFE_LINK_REJECTION_REASONS = frozenset({
    "request binding does not match authenticated deployment",
    "peer certificate is not authorized for this binding and role",
    "authenticated TLS peer certificate is missing",
    "link binding is not active",
})

# Manager 路由与用户 Web 的 /api/v1/auth/* 等共用前缀，鉴权只拦管理面 path。
_MANAGER_PATH_PREFIXES = (
    "/api/v1/agent-templates",
    "/api/v1/model-templates",
    "/api/v1/embedding-templates",
    "/api/v1/mcp-templates",
    "/api/v1/permissions-templates",
    "/api/v1/skill-prebuilt-templates",
    "/api/v1/extension-config-templates",
    "/api/v1/a2a-outbound-templates",
    "/api/v1/a2a-access-policies",
    "/api/v1/instance-agent-resources",
    "/api/v1/instance-data-lifecycle",
    "/api/v1/workspace-quota/",
    "/api/v1/logging",
    "/api/v1/task-memory",
    "/api/v1/memory",
    "/api/v1/cron-policy",
    "/api/v1/audit-log",
    "/api/v1/log-masking-rules",
)


def _is_manager_config_path(path: str) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in _MANAGER_PATH_PREFIXES
    )


def register_manager_config_routes(app: FastAPI) -> bool:
    """Register Manager config routers on an existing FastAPI app.

    Returns True when routes were registered.
    """
    if not is_enterprise():
        logger.info("[ManagerConfig] skipped: not enterprise edition")
        return False

    link_mtls = LinkMTLSConfig.from_env()

    @app.middleware("http")
    async def link_binding_guard(request: Request, call_next):
        path = request.url.path or ""
        if not _is_manager_config_path(path):
            return await call_next(request)
        try:
            link_mtls.authorize_request(request)
        except LinkMTLSError as exc:
            reason = str(exc)
            safe_reason = (
                reason
                if reason in _SAFE_LINK_REJECTION_REASONS
                else "link authorization failed"
            )
            logger.warning(
                "[ManagerConfig] rejected link binding: %s",
                safe_reason,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": {
                        "code": "LINK_BINDING_MISMATCH",
                        "message": safe_reason,
                    },
                },
            )
        return await call_next(request)

    @app.get("/api/health", tags=["System"])
    async def system_health() -> dict[str, str]:
        return {"status": "ok"}

    v1 = APIRouter(prefix="/api/v1")

    @v1.get("/ready", tags=["System"])
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    v1.include_router(templates_router, tags=["Templates"])
    v1.include_router(instance_router, tags=["Instances"])
    v1.include_router(instance_resource_router, tags=["Instance Resources"])
    v1.include_router(application_config_router, tags=["Application Config"])
    v1.include_router(quota_router, tags=["Quota"])
    app.include_router(v1)
    logger.info("[ManagerConfig] routes registered on unified WEB_PORT app")
    return True
