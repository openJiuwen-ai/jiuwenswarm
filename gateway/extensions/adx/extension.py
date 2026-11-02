# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Discoverable ADX Gateway transport extension."""

from gateway.common.config import get_config
from gateway.extensions.adx.config import adx_router_selected, load_router_config
from gateway.extensions.adx.router_client import AdxRouterClient
from gateway.extensions.agentos.agentos_router.agentos_authenticator import (
    AgentOSAuthenticator,
)
from gateway.extensions.sdk.agent_server_client import AgentServerClientExtension
from gateway.extensions.sdk.third_agent import ThirdAgentExtension
from gateway.extensions.agentos.agentos_router.third_agent import AgentOSThirdAgent


class AdxRouter(AgentServerClientExtension, ThirdAgentExtension):
    def __init__(self, config) -> None:
        settings = config.router
        auth_client = (
            AgentOSAuthenticator(settings.auth_service_url, settings.timeout)
            if settings.auth_enabled else None
        )
        self._client = AdxRouterClient(config, auth_client=auth_client)
        self._third_agent = AgentOSThirdAgent(self._client)

    async def initialize(self, config) -> None:
        del config

    def get_client(self) -> AdxRouterClient:
        return self._client

    def get_third_agent(self):
        return self._third_agent

    def set_key_issuer(self, key_issuer, *, ephemeral_key_ttl_sec: float = 300.0) -> None:
        self._client.set_key_issuer(key_issuer, ephemeral_key_ttl_sec=ephemeral_key_ttl_sec)

    async def resolve_web_endpoint(self, user_id: str, agent_type: str, protocol: str,
                                   *, acquire: bool = False) -> str | None:
        return await self._client.resolve_web_endpoint(user_id, agent_type, protocol, acquire=acquire)

    async def release_web_endpoint(self, user_id: str, agent_type: str) -> None:
        await self._client.release_web_endpoint(user_id, agent_type)

    async def shutdown(self) -> None:
        await self._client.shutdown()


async def register_extensions(registry):
    config = get_config()
    if not adx_router_selected(config):
        return []
    extension = AdxRouter(load_router_config(config))
    registry.register_agent_server_client(extension)
    registry.register_third_agent(extension)
    return [extension]
