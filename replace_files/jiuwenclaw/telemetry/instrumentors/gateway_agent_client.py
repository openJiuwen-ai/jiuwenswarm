# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Instrumentor for Gateway -> AgentServer client spans."""

from __future__ import annotations

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.agent_client_proxy import (
    GATEWAY_CLIENT_INSTRUMENTED_ATTR,
    _send_request_stream_with_telemetry,
    _send_request_with_telemetry,
    wrap_agent_client_extension,
)

_EXTENSION_REGISTRY_INSTRUMENTED_ATTR = "_jiuwenclaw_agent_client_extension_registry_instrumented"


def instrument_gateway_agent_client() -> None:
    """Patch Gateway client creation paths so telemetry stays inside the telemetry package."""
    _instrument_builtin_gateway_client()
    _instrument_agent_client_extension_registration()


def _instrument_builtin_gateway_client() -> None:
    try:
        from jiuwenclaw.gateway.agent_client import WebSocketAgentServerClient
    except ImportError:
        logger.debug("[Telemetry] WebSocketAgentServerClient not available, skipping gateway client instrumentor")
        return

    if getattr(WebSocketAgentServerClient, GATEWAY_CLIENT_INSTRUMENTED_ATTR, False):
        return

    _original_send_request = WebSocketAgentServerClient.send_request
    _original_send_request_stream = WebSocketAgentServerClient.send_request_stream

    async def _traced_send_request(self, envelope):
        async def _invoke(inner_envelope):
            return await _original_send_request(self, inner_envelope)

        return await _send_request_with_telemetry(
            _invoke,
            envelope=envelope,
            target_uri=getattr(self, "_uri", None),
        )

    async def _traced_send_request_stream(self, envelope):
        async def _invoke(inner_envelope):
            async for chunk in _original_send_request_stream(self, inner_envelope):
                yield chunk

        async for chunk in _send_request_stream_with_telemetry(
            _invoke,
            envelope=envelope,
            target_uri=getattr(self, "_uri", None),
        ):
            yield chunk

    WebSocketAgentServerClient.send_request = _traced_send_request
    WebSocketAgentServerClient.send_request_stream = _traced_send_request_stream
    setattr(WebSocketAgentServerClient, GATEWAY_CLIENT_INSTRUMENTED_ATTR, True)


def _instrument_agent_client_extension_registration() -> None:
    try:
        from jiuwenclaw.extensions.registry import ExtensionRegistry
    except ImportError:
        logger.debug("[Telemetry] ExtensionRegistry not available, skipping agent client extension instrumentor")
        return

    if getattr(ExtensionRegistry, _EXTENSION_REGISTRY_INSTRUMENTED_ATTR, False):
        return

    _original_register = ExtensionRegistry.register_agent_server_client

    def _traced_register(self, extension):
        return _original_register(self, wrap_agent_client_extension(extension))

    ExtensionRegistry.register_agent_server_client = _traced_register
    setattr(ExtensionRegistry, _EXTENSION_REGISTRY_INSTRUMENTED_ATTR, True)

    # Fix: Check if extension was already registered before telemetry init
    # (happens when load_all_extensions runs before init_telemetry in app_gateway.py)
    try:
        # Only wrap if get_instance() exists and registry is initialized
        if hasattr(ExtensionRegistry, "get_instance"):
            registry = ExtensionRegistry.get_instance()
            existing_ext = registry._agent_server_client
            if existing_ext is not None:
                logger.info("[Telemetry] wrapping already-registered AgentServerClient extension")
                registry._agent_server_client = wrap_agent_client_extension(existing_ext)
    except RuntimeError:
        # Registry not yet initialized - no existing extension to wrap
        logger.debug("[Telemetry] ExtensionRegistry not initialized, no existing extension to wrap")
