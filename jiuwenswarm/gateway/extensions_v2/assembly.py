# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway assembly for Extensions V2: event bus, runtime and lifecycle.

The Gateway mirrors AgentServer's assembly: one process-wide object is built
from the resolved configuration, a startup failure disables the feature
instead of the Gateway, and shutdown releases every handler it registered.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig, load_gateway_config
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime
from jiuwenswarm.gateway.extensions_v2.state import UiStateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GatewayAssembly:
    """Objects owned by one Gateway process for the lifetime of the channel."""

    bus: ExtensionEventBus
    runtime: GatewayRuntime
    store: UiStateStore


_ASSEMBLY: GatewayAssembly | None = None
_LOCK = asyncio.Lock()
_LOCAL_CLI: Any = None


async def install_gateway_extensions(
    channel: Any,
    config: GatewayExtensionConfig | Mapping[str, Any] | None = None,
) -> GatewayAssembly | None:
    """Assemble Extensions V2 once; a failure disables the feature, not the Gateway."""

    global _ASSEMBLY, _LOCAL_CLI
    async with _LOCK:
        if _ASSEMBLY is not None:
            return _ASSEMBLY
        settings = config if isinstance(config, GatewayExtensionConfig) else load_gateway_config(config)
        if not settings.enabled:
            logger.info("[GatewayExtensions] disabled by configuration")
            return None
        assembly = _build_assembly(channel, settings)
        try:
            await assembly.runtime.start()
        except Exception:  # noqa: BLE001 - extensions must not block the Gateway
            logger.exception("[GatewayExtensions] assembly failed; extension features are unavailable")
            await assembly.runtime.shutdown()
            return None
        _hook_disconnect(channel)
        _ASSEMBLY = assembly
        try:
            from jiuwenswarm.gateway.extensions_v2.management.local_cli import start_local_cli

            _LOCAL_CLI = await start_local_cli(assembly.runtime)
        except Exception:  # noqa: BLE001 - CLI unavailability must not disable the Gateway
            logger.exception("[GatewayExtensions] local CLI unavailable")
        logger.info("[GatewayExtensions] assembly ready")
        return assembly


async def shutdown_gateway_extensions() -> None:
    """Stop the runtime and drop its handlers; idempotent."""

    global _ASSEMBLY, _LOCAL_CLI
    async with _LOCK:
        assembly, _ASSEMBLY = _ASSEMBLY, None
    if assembly is not None:
        if _LOCAL_CLI is not None:
            await _LOCAL_CLI.close()
            _LOCAL_CLI = None
        await assembly.runtime.shutdown()


def get_gateway_assembly() -> GatewayAssembly | None:
    """Return the active assembly, or None when extensions are disabled."""

    return _ASSEMBLY


def get_extension_bus() -> ExtensionEventBus | None:
    """Return the Gateway bus, or None when extensions are disabled."""

    return _ASSEMBLY.bus if _ASSEMBLY is not None else None


def get_gateway_runtime() -> GatewayRuntime | None:
    """Return the active runtime, or None when extensions are disabled."""

    return _ASSEMBLY.runtime if _ASSEMBLY is not None else None


def _build_assembly(channel: Any, settings: GatewayExtensionConfig) -> GatewayAssembly:
    runtime = GatewayRuntime(channel=channel, config=settings)
    return GatewayAssembly(bus=runtime.bus, runtime=runtime, store=runtime.store)


def _hook_disconnect(channel: Any) -> None:
    """Subscribe the runtime to the channel's disconnect callback once."""

    if getattr(channel, "_gateway_extensions_hooked", False):
        return
    register = getattr(channel, "on_disconnect", None)
    if not callable(register):
        return
    from jiuwenswarm.gateway.extensions_v2 import notify_gateway_disconnect

    register(notify_gateway_disconnect)
    connect = getattr(channel, "on_connect", None)
    if callable(connect):
        async def browser_connected(ws):
            runtime = get_gateway_runtime()
            if runtime is not None and runtime.browser.enabled:
                connection_id = await runtime.connection_id(ws)
                await runtime.browser.connect(ws, connection_id)
        connect(browser_connected)
    setattr(channel, "_gateway_extensions_hooked", True)
