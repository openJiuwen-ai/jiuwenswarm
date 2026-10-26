# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServerClient 扩展."""

from __future__ import annotations

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.sdk import AgentServerClientExtension
from jiuwenswarm.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient


class YuanrongAgentServerClientExtension(AgentServerClientExtension):
    """openYuanRong Frontend AgentServerClient 扩展."""

    def __init__(self, client: YuanrongFrontendAgentClient) -> None:
        self._client = client

    async def initialize(self, config) -> None:
        return None

    def get_client(self) -> YuanrongFrontendAgentClient:
        return self._client


async def register_extensions(registry):
    """注册 AgentServerClient 扩展.

    根据 gateway.agent_client.type 配置决定是否注册。
    如果 type 为 "yuanrong"，则创建并注册 YuanrongAgentServerClientExtension。
    """
    cfg = get_config()
    gateway = cfg.get("gateway") if isinstance(cfg, dict) else {}
    agent_client = gateway.get("agent_client") if isinstance(gateway, dict) else {}
    if not isinstance(agent_client, dict):
        return []

    client_type = str(agent_client.get("type") or "websocket").strip().lower()
    if client_type != "yuanrong":
        return []

    frontend_endpoint = str(agent_client.get("frontend_endpoint") or "").strip()
    function_version_urn = str(agent_client.get("function_version_urn") or "").strip()
    agent_namespace = str(agent_client.get("agent_namespace") or "default").strip() or "default"
    if not frontend_endpoint or not function_version_urn:
        raise ValueError(
            "gateway.agent_client.frontend_endpoint and function_version_urn are required in yuanrong mode"
        )

    ext = YuanrongAgentServerClientExtension(
        YuanrongFrontendAgentClient(
            frontend_endpoint=frontend_endpoint,
            function_version_urn=function_version_urn,
            concurrency=int(agent_client.get("concurrency") or 1),
            invoke_timeout_s=float(agent_client.get("invoke_timeout_s") or 60.0),
            agent_timeout_s=float(agent_client.get("agent_timeout_s") or 300.0),
            agent_namespace=agent_namespace,
        )
    )
    registry.register_agent_server_client(ext)
    return [ext]
