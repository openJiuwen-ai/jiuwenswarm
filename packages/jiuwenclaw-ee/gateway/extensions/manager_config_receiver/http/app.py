# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jiuwenswarm.common.security.link_mtls import (
    LinkMTLSConfig,
    LinkMTLSError,
)

from ..infrastructure.config import get_settings
from ..routers.application_config_routers import application_config_router
from ..routers.instance_resource_routers import instance_resource_router
from ..routers.instance_routers import instance_router
from ..routers.template_routers import templates_router

logger = logging.getLogger(__name__)

# Log only fixed reasons, never arbitrary exception text or request data.
_SAFE_LINK_REJECTION_REASONS = frozenset({
    "request binding does not match authenticated deployment",
    "peer certificate is not authorized for this binding and role",
    "authenticated TLS peer certificate is missing",
    "link binding is not active",
})


def create_app() -> FastAPI:
    """Gateway 本机配置接收接口（每网关独立 DB，无路径级实例段）。"""
    app = FastAPI(title="Gateway Manager Config Receiver", docs_url="/docs", redoc_url=None)
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=get_settings().gateway_config_forwarded_allow_ips,
    )
    link_mtls = LinkMTLSConfig.from_env()

    @app.middleware("http")
    async def link_binding_guard(request: Request, call_next):
        try:
            link_mtls.authorize_request(request)
        except LinkMTLSError as exc:
            reason = str(exc)
            logger.warning(
                "[ManagerConfigReceiver] rejected link binding: %s",
                reason if reason in _SAFE_LINK_REJECTION_REASONS else "link authorization failed",
            )
            return JSONResponse(status_code=403, content={
                "ok": False, "error": {"code": "LINK_BINDING_MISMATCH", "message": str(exc)},
            })
        return await call_next(request)

    @app.get("/api/health", tags=["System"])
    async def system_health() -> dict[str, str]:
        """通用健康检查（Manager 探活 / 负载均衡 / K8s liveness）。"""
        return {"status": "ok"}

    v1 = APIRouter(prefix="/api/v1")

    @v1.get("/ready", tags=["System"])
    async def ready() -> dict[str, str]:
        """K8s readiness（部署模板 ``readinessProbe`` 探此路径）。"""
        return {"status": "ready"}

    v1.include_router(templates_router, tags=["Templates"])
    v1.include_router(instance_router, tags=["Instances"])
    v1.include_router(instance_resource_router, tags=["Instance Resources"])
    v1.include_router(application_config_router, tags=["Application Config"])

    app.include_router(v1)
    return app
