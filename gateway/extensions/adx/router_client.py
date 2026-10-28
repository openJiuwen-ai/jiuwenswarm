# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reuse Gateway business routing with ADX Environment lifecycle and forwarding."""
from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from openjiuwen_protocol.e2a.agent_models import AgentResponse
from openjiuwen_protocol.e2a.models import E2AEnvelope

from gateway.channel_manager.base import ChannelType
from gateway.extensions.adx.config import AdxRouterConfig
from gateway.extensions.adx.control_client import AdxControlClient
from gateway.extensions.adx.errors import AdxRoutingError
from gateway.extensions.adx.file_client import AdxFileMixin
from gateway.extensions.agentos.agentos_router.agent_manager import AgentManager
from gateway.extensions.agentos.agentos_router.agentos_authenticator import (
    AgentOSAuthenticator,
)
from gateway.extensions.agentos.agentos_router.registry_client import RegistryClient
from gateway.extensions.agentos.agentos_router.router_client import (
    AgentOSRouterClient,
    is_ws_connect_retryable,
    ws_connect_ready_timeout_seconds,
    ws_connect_retry_interval_seconds,
)
from gateway.extensions.auth.credential_authenticator import AuthResult
from gateway.extensions.yuanrong_frontend_client import apply_trace_header
from gateway.health_check import HEALTH_CHECK_CHANNEL_ID, HEALTH_CHECK_OK


logger = logging.getLogger(__name__)


class AdxRouterClient(AdxFileMixin, AgentOSRouterClient):
    """Keep Registry, launch-spec assembly and user routing in the business Gateway."""

    def __init__(self, config: AdxRouterConfig, *, ws_client_factory: Callable | None = None,
                 control_client: AdxControlClient | None = None,
                 registry_client: RegistryClient | None = None,
                 auth_client: AgentOSAuthenticator | None = None) -> None:
        self._config = config
        self._control = control_client or AdxControlClient(config)
        settings = config.router
        super().__init__(
            self._control, registry_client or RegistryClient(settings.registry),
            AgentManager(
                creating_timeout_seconds=settings.creating_timeout_seconds, key_fields=settings.agent_key_fields,
            ),
            workspace_root=settings.workspace_root, agent_sandbox_cpu=settings.agent_sandbox_cpu,
            agent_sandbox_memory=settings.agent_sandbox_memory,
            sandbox_idle_timeout_seconds=settings.sandbox_idle_timeout_seconds,
            sandbox_idle_check_interval_seconds=settings.sandbox_idle_check_interval_seconds,
            disconnect_cleanup_timeout_seconds=settings.disconnect_cleanup_timeout_seconds,
            connect_warmup_enabled=False, probe_settings=settings.probes, auth_client=auth_client,
            ws_client_factory=ws_client_factory,
        )
        self._closed = True
        self.set_or_update_server_config(config={})

    async def shutdown(self) -> None:
        await super().shutdown()
        await self._control.close()

    def set_channel_manager(self, channel_manager) -> None:
        super().set_channel_manager(channel_manager)
        channel = channel_manager.get_channel(ChannelType.WEB)
        if channel is not None:
            channel.container_file_client = self

    async def authenticate_http(self, *, path: str, headers: Mapping[str, str], remote: str = '',
                                channel: str = 'file-api', allow_query_token: bool = True) -> AuthResult:
        if channel == 'file-api':
            self._begin_file_transfer()
        return await super().authenticate_http(
            path=path, headers=headers, remote=remote, channel=channel,
            allow_query_token=allow_query_token,
        )

    def set_or_update_server_config(self, *, config: dict, env: dict | None = None) -> None:
        settings = copy.deepcopy(config)
        settings.setdefault('sandbox', {})['enabled'] = False
        super().set_or_update_server_config(config=settings, env=env)

    def _tool_sandbox_env(self) -> dict[str, str]:
        return {}

    def _agent_url(self, instance: str, port: int, protocol: str) -> str:
        origin = urlsplit(self._config.ingress_url)
        target = 'urn:adx:instance:' + quote(instance, safe='')
        return urlunsplit(('wss' if protocol == 'ws' else 'https', origin.netloc,
                          f'/agent/{protocol}', urlencode({'target': target, 'port': port}), ''))

    def _agent_ws_url(self, instance_id: str, agent_port: int) -> str:
        return self._agent_url(instance_id, agent_port, 'ws')

    def _agent_http_url(self, instance_id: str, agent_port: int) -> str:
        return self._agent_url(instance_id, agent_port, 'http')

    def _agent_web_ws_url(self, instance_id: str, agent_port: int) -> str:
        return self._agent_ws_url(instance_id, agent_port)

    async def _connect_ws_until_ready(self, *, instance_id: str, agent_port: int,
                                      user_id: str = '', session_id: str = '', agent_type: str = ''):
        uri = self._agent_ws_url(instance_id, agent_port)
        deadline = asyncio.get_running_loop().time() + ws_connect_ready_timeout_seconds()
        headers = apply_trace_header({'Authorization': 'Bearer ' + self._config.token})
        while True:
            client = self._ws_client_factory()
            if self._push_handler is not None:
                client.set_server_push_handler(self._push_handler)
            try:
                await client.connect(uri, extra_headers=headers)
                self._register_ws_disconnect_handler(client, user_id=user_id, session_id=session_id,
                                                     agent_type=agent_type, instance_id=instance_id)
                return client
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(client.disconnect())
                except BaseException:
                    logger.warning('ADX WebSocket cancellation cleanup failed', exc_info=True)
                raise
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    logger.warning('ADX WebSocket cleanup failed', exc_info=True)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or not is_ws_connect_retryable(exc):
                    raise
                await asyncio.sleep(min(ws_connect_retry_interval_seconds(), remaining))

    async def send_request(self, envelope: E2AEnvelope, *, timeout: float | None = None) -> AgentResponse:
        if envelope.channel == HEALTH_CHECK_CHANNEL_ID:
            if self._closed:
                raise AdxRoutingError('ADX router is closed', code='ADX_ROUTER_CLOSED')
            return AgentResponse(
                envelope.request_id, envelope.channel,
                payload={"health_check": f"{HEALTH_CHECK_OK}: mocked by adx_router"},
            )
        return await super().send_request(envelope, timeout=timeout)
