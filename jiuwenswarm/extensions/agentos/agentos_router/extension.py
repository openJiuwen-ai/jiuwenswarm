# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentManager
from jiuwenswarm.extensions.agentos.agentos_router.agentos_authenticator import AgentOSAuthenticator
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    RouterConfig,
    agentos_router_selected,
    load_router_config,
)
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import RegistryClient
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import YuanrongSshRelay
from jiuwenswarm.extensions.agentos.agentos_router.third_agent import AgentOSThirdAgent
from jiuwenswarm.extensions.sdk.agent_server_client import (
    AgentServerClientExtension,
)
from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    YuanrongFrontendAgentClient,
)
from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.gateway.routing.third_agent import ThirdAgent


class AgentOSRouter(AgentServerClientExtension, ThirdAgentExtension):
    """AgentOS southbound Router extension (AgentServerClient + ThirdAgent)."""

    def __init__(self, config: RouterConfig) -> None:
        self._config = config
        self._yuanrong_client = YuanrongFrontendAgentClient(
            frontend_endpoint=config.frontend_endpoint,
            function_version_urn=config.function_version_urn,
            concurrency=config.concurrency,
            invoke_timeout_s=config.invoke_timeout_s,
            agent_timeout_s=config.agent_timeout_s,
            agent_namespace=config.agent_namespace,
        )
        self._registry_client = RegistryClient(config.registry)
        self._agent_manager = AgentManager(
            creating_timeout_seconds=config.creating_timeout_seconds,
            key_fields=config.agent_key_fields,
        )
        self._ssh_relay = YuanrongSshRelay(
            config.ssh,
            frontend_endpoint=config.frontend_endpoint,
        )
        self._router_client = AgentOSRouterClient(
            self._yuanrong_client,
            self._registry_client,
            self._agent_manager,
            ssh_relay=self._ssh_relay,
            ssh_channel_endpoint=config.ssh_channel,
            workspace_root=config.workspace_root,
            sandbox_idle_timeout_seconds=config.sandbox_idle_timeout_seconds,
            sandbox_idle_check_interval_seconds=(
                config.sandbox_idle_check_interval_seconds
            ),
            disconnect_cleanup_timeout_seconds=(
                config.disconnect_cleanup_timeout_seconds
            ),
            auth_client=AgentOSAuthenticator(config.auth_service_url, config.timeout) if config.auth_enabled else None
        )
        self._third_agent = AgentOSThirdAgent(self._router_client)
        self._closed = False

    async def initialize(self, config) -> None:
        del config

    def get_client(self) -> AgentServerClient:
        return self._router_client

    def get_third_agent(self) -> ThirdAgent:
        return self._third_agent

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
    extension = AgentOSRouter(load_router_config(config))
    registry.register_agent_server_client(extension)
    registry.register_third_agent(extension)
    return [extension]
