# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# agent_ssas/core/framework/access_adapter/agent_remote_backend.py
"""AgentSSAS HTTP 模式接入适配插件。

AgentSSASRemoteBackend 在宿主进程内实现 AgentSSASBackendProtocol,
通过 httpx.AsyncClient 将 report_event 转发为 HTTP POST 调用到
服务端 FastAPI 进程。网络异常或超时时 fail-open 返回无风险
RiskAssessment(risk_level=Safe),不阻断业务。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel

logger = logging.getLogger(__name__)


class AgentSSASRemoteBackend:
    """HTTP 模式接入适配插件。

    通过 httpx.AsyncClient 将 report_event(raw_event) 转发为
    POST /api/v1/events HTTP 调用到服务端。对 AgentSSASSecurityRail 透明,
    与进程内模式 AgentSSASBackend 暴露相同接口。

    网络异常、超时或服务端错误时 fail-open 返回无风险 RiskAssessment。
    不需要 initialize()(检测模块在服务端初始化)。
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化 HTTP 模式接入适配插件。

        Args:
            config: AgentSSASCore 子系统配置,使用 http_endpoint、
                http_timeout、http_token 等字段。
        """
        self._config = config
        headers: dict[str, str] = {}
        if config.http_token:
            headers["Authorization"] = f"Bearer {config.http_token}"
        self._client = httpx.AsyncClient(
            base_url=config.http_endpoint,
            timeout=config.http_timeout,
            headers=headers or None,
        )

    async def initialize(self) -> None:
        """HTTP 模式客户端无需初始化检测模块。

        检测模块在服务端 FastAPI 进程中初始化,客户端只负责转发。
        此方法为空实现,保持与 AgentSSASBackend 接口一致。
        """
        logger.info("AgentSSASRemoteBackend 初始化完成(检测模块在服务端)")

    async def report_event(self, raw_event: dict) -> RiskAssessment:
        """通过 HTTP 上报事件,返回风险评估结果。

        将 raw_event 作为 POST /api/v1/events 请求体发送到服务端,
        解析响应为 RiskAssessment。网络异常或超时时 fail-open 返回
        无风险 RiskAssessment(risk_level=Safe)。

        Args:
            raw_event: 原始事件 dict(三层结构:common / payload / metadata)。

        Returns:
            服务端返回的 RiskAssessment。自身运行故障时返回无风险结果。
        """
        try:
            resp = await self._client.post(
                "/api/v1/events",
                json={"raw_event": raw_event},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            assessment_data = data.get("assessment", {})
            return RiskAssessment.from_dict(assessment_data)
        except Exception:
            logger.exception(
                "HTTP report_event 失败,fail-open 返回无风险结果"
            )
            return RiskAssessment(risk_level=RiskLevel.SAFE)

    async def close(self) -> None:
        """关闭 HTTP 客户端连接。"""
        await self._client.aclose()
