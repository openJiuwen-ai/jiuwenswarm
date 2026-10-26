# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Discoverable ADX Gateway transport extension."""

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.adx.config import adx_router_selected, load_router_config
from jiuwenswarm.extensions.adx.router_client import AdxRouterClient
from jiuwenswarm.extensions.sdk.agent_server_client import AgentServerClientExtension


class AdxRouter(AgentServerClientExtension):
    def __init__(self, config) -> None:
        self._client = AdxRouterClient(config)

    async def initialize(self, config) -> None:
        del config

    def get_client(self) -> AdxRouterClient:
        return self._client

    async def shutdown(self) -> None:
        await self._client.shutdown()


async def register_extensions(registry):
    config = get_config()
    if not adx_router_selected(config):
        return []
    extension = AdxRouter(load_router_config(config))
    registry.register_agent_server_client(extension)
    return [extension]
