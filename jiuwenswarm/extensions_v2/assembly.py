# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer assembly for Extensions V2: event bus, packages and WebUI."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2 import bootstrap
from jiuwenswarm.extensions_v2.config import resolve_install_dir
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtensionAssembly:
    """Objects owned by one AgentServer process for the lifetime of the runtime."""

    bus: ExtensionEventBus
    manager: ExtensionManager
    webui: WebUIRuntime
    transport: RenderTransport


_ASSEMBLY: ExtensionAssembly | None = None
_LOCK = asyncio.Lock()


async def install_extensions(server: Any = None) -> ExtensionAssembly | None:
    """Assemble Extensions V2 once; a failure disables the feature, not the server."""

    global _ASSEMBLY
    async with _LOCK:
        if _ASSEMBLY is not None:
            return _ASSEMBLY
        try:
            assembly = _build_assembly(server)
            await assembly.manager.init(
                assembly.bus,
                host_version=_host_version(),
                host_event_types=lambda bus: bootstrap.register_event_types(bus, assembly.webui),
                host_handlers=lambda bus: bootstrap.register_handlers(bus, assembly.webui),
            )
        except Exception:  # noqa: BLE001 - extensions must not block chat startup
            logger.exception("[ExtensionsV2] assembly failed; extension features are unavailable")
            return None

        await _activate_installed(assembly.manager)
        _ASSEMBLY = assembly
        logger.info("[ExtensionsV2] assembly ready")
        return assembly


def get_extension_manager() -> ExtensionManager | None:
    return _ASSEMBLY.manager if _ASSEMBLY is not None else None


def get_extension_bus() -> ExtensionEventBus | None:
    return _ASSEMBLY.bus if _ASSEMBLY is not None else None


def get_webui_runtime() -> WebUIRuntime | None:
    return _ASSEMBLY.webui if _ASSEMBLY is not None else None


async def shutdown_extensions() -> None:
    """Release active views and package handlers without deleting install copies."""

    global _ASSEMBLY
    async with _LOCK:
        assembly = _ASSEMBLY
        if assembly is None:
            return
        await assembly.manager.shutdown()
        await assembly.webui.shutdown()
        _ASSEMBLY = None


def _build_assembly(server: Any) -> ExtensionAssembly:
    bus = ExtensionEventBus()
    manager = ExtensionManager(resolve_install_dir())
    transport = RenderTransport(push=_push_from(server))
    catalog = WebUICatalogService(manager.active_packages)
    webui = WebUIRuntime(bus=bus, transport=transport, catalog=catalog)
    manager.set_inactive_callback(webui.drop_package)
    return ExtensionAssembly(bus=bus, manager=manager, webui=webui, transport=transport)


async def _activate_installed(manager: ExtensionManager) -> None:
    try:
        await manager.load_installed()
    except Exception:  # noqa: BLE001 - one bad package must not disable the rest
        logger.exception("[ExtensionsV2] failed to activate installed packages")


def _push_from(server: Any) -> Any:
    return server if callable(getattr(server, "send_push", None)) else None


def _host_version() -> str | None:
    try:
        return version("jiuwenswarm")
    except PackageNotFoundError:
        return None
