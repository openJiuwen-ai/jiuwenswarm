# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# agent_ssas/core/framework/access_adapter/http_server.py
"""AgentSSAS HTTP 服务端(FastAPI)。

暴露 POST /api/v1/events 端点,接收 AgentSSASSecurityRail 通过
AgentSSASRemoteBackend 转发的事件,调用进程内 AgentSSASBackend
执行实际分析,返回 RiskAssessment。

当 AgentSSASConfig.http_token 不为 None 时,启用 Bearer token 认证中间件,
未携带有效 Authorization 头的请求返回 401。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig

logger = logging.getLogger(__name__)

# 免认证路径集合(健康检查等运维端点)
_NO_AUTH_PATHS: frozenset[str] = frozenset({"/health"})


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Bearer token 认证中间件。

    当 AgentSSASConfig.http_token 不为 None 时,校验请求的 Authorization 头
    是否为 "Bearer <token>"。未携带或不匹配时返回 401。
    /health 端点免认证。
    """

    async def dispatch(self, request: Request, call_next):
        """校验 Authorization 头。"""
        if request.url.path in _NO_AUTH_PATHS:
            return await call_next(request)

        config: AgentSSASConfig | None = getattr(request.app.state, "ssas_config", None)
        expected_token = config.http_token if config else None
        if expected_token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {expected_token}":
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                )
        return await call_next(request)


class EventRequest(BaseModel):
    """事件上报请求体。"""

    raw_event: dict[str, Any]


class AssessmentResponse(BaseModel):
    """风险评估响应体。"""

    assessment: dict[str, Any]


def create_app(config: AgentSSASConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用实例。

    Args:
        config: AgentSSASCore 子系统配置。None 时使用默认配置。
            当 config.http_token 不为 None 时启用 Bearer token 认证。

    Returns:
        FastAPI 应用实例,已注册 POST /api/v1/events 端点。
    """
    if config is None:
        config = AgentSSASConfig()

    app = FastAPI(
        title="AgentSSAS HTTP Server",
        description="AgentSSAS HTTP 服务模式 — 事件上报与风险评估",
        version="0.1.0",
    )

    # 使用 app.state 存储后端实例,支持懒初始化
    app.state.ssas_config = config
    app.state.ssas_backend: AgentSSASBackend | None = None

    # 当配置了 http_token 时,启用 Bearer token 认证中间件
    if config.http_token is not None:
        app.add_middleware(TokenAuthMiddleware)
        logger.info("SSAS HTTP 服务端启用 Bearer token 认证")

    @app.post("/api/v1/events", response_model=AssessmentResponse)
    async def report_event(request: EventRequest) -> AssessmentResponse:
        """上报 raw_event,返回风险评估结果。

        对应 AgentSSASBackendProtocol.report_event 的 HTTP 映射。
        事件类型由 raw_event.common.event_type + event_class 字段区分。
        """
        backend: AgentSSASBackend | None = app.state.ssas_backend
        if backend is None:
            # 懒初始化:首次请求时创建并初始化后端
            backend = AgentSSASBackend(app.state.ssas_config)
            await backend.initialize()
            app.state.ssas_backend = backend
            logger.info("SSAS HTTP 服务端后端懒初始化完成")
        assessment = await backend.report_event(request.raw_event)
        return AssessmentResponse(assessment=assessment.to_dict())

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """健康检查端点(免认证)。"""
        return {"status": "ok"}

    return app


async def initialize_app(app: FastAPI, config: AgentSSASConfig) -> None:
    """显式初始化应用后端(用于非懒初始化场景)。

    Args:
        app: FastAPI 应用实例。
        config: AgentSSASCore 子系统配置。
    """
    backend = AgentSSASBackend(config)
    await backend.initialize()
    app.state.ssas_backend = backend
    app.state.ssas_config = config
    logger.info("SSAS HTTP 服务端后端初始化完成")
