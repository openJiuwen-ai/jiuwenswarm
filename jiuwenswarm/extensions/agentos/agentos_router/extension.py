# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.client.agent_client import AgentServerClient
from jiuwenswarm.extensions.sdk.agent_server_client import AgentServerClientExtension
from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
from jiuwenswarm.common.client.third_agent import ThirdAgent

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.agentos.agentos_router.agentos_authenticator import AgentOSAuthenticator
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    agentos_router_selected,
)
from jiuwenswarm.extensions.agentos.agentos_router.adx.config import (
    AdxRouterConfig, load_router_config as load_adx_config,
)
from jiuwenswarm.extensions.agentos.agentos_router.third_agent import AgentOSThirdAgent


class AgentOSRouter(AgentServerClientExtension, ThirdAgentExtension):
    """AgentOS southbound Router extension (AgentServerClient + ThirdAgent)."""

    def __init__(self, config: AdxRouterConfig) -> None:
        from jiuwenswarm.extensions.agentos.agentos_router.adx.router_client import AdxRouterClient
        self._config = config
        settings = config.router
        auth_client = (AgentOSAuthenticator(settings.auth_service_url, settings.timeout)
                       if settings.auth_enabled else None)
        self._router_client = AdxRouterClient(config, auth_client=auth_client)
        self._third_agent = AgentOSThirdAgent(self._router_client)
        self._closed = False

    async def initialize(self, config) -> None:
        del config

    def get_client(self) -> AgentServerClient:
        return self._router_client

    def get_third_agent(self) -> ThirdAgent:
        return self._third_agent

    async def resolve_web_endpoint(
        self,
        user_id: str,
        agent_type: str,
        protocol: str,
        *,
        acquire: bool = False,
        trace_id: str | None = None,
    ) -> str | None:
        """Delegate to router client for the WebChannel agent UI proxy."""
        kwargs: dict[str, Any] = {"acquire": acquire}
        if trace_id:
            kwargs["trace_id"] = trace_id
        return await self._router_client.resolve_web_endpoint(
            user_id, agent_type, protocol, **kwargs
        )

    async def release_web_endpoint(self, user_id: str, agent_type: str) -> None:
        """Drop one web-proxy task hold taken by ``resolve_web_endpoint``."""
        await self._router_client.release_web_endpoint(user_id, agent_type)

    async def acquire_web_endpoint(
        self, user_id: str, agent_type: str, protocol: str, *, trace_id: str | None = None,
    ):
        """Resolve an endpoint with a generation-specific activity hold."""
        kwargs: dict[str, Any] = {"acquire": True, "return_hold": True}
        if trace_id:
            kwargs["trace_id"] = trace_id
        return await self._router_client.resolve_web_endpoint(
            user_id, agent_type, protocol, **kwargs
        )

    def set_key_issuer(
        self,
        key_issuer,
        *,
        ephemeral_key_ttl_sec: float = 300.0,
    ) -> None:
        """Inject AgentOS SSH key issuer (or clear)."""
        self._router_client.set_key_issuer(
            key_issuer,
            ephemeral_key_ttl_sec=ephemeral_key_ttl_sec,
        )

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._router_client.shutdown()


async def register_extensions(registry):
    config = get_config()
    if not agentos_router_selected(config):
        return []
    settings = load_adx_config(config)
    extension = AgentOSRouter(settings)
    registry.register_agent_server_client(extension)
    registry.register_third_agent(extension)
    return [extension]
