# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer assembly for Extensions V2: event bus, packages and WebUI."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2 import bootstrap
from jiuwenswarm.common.version import get_runtime_version
from jiuwenswarm.extensions_v2.session_authorization.service import AuthorizationCaller, SessionAuthorizationService
from jiuwenswarm.extensions_v2.session_compaction.service import SessionCompactionService
from jiuwenswarm.extensions_v2.config import (
    InstallReportSettings,
    resolve_install_dir, resolve_state_dir, resolve_store_dir,
    resolve_install_report_path, resolve_install_report_settings,
)
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartEventBus
from jiuwenswarm.extensions_v2.management import ExtensionManager
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport
from jiuwenswarm.common.system_plugin_recovery import begin_system_plugin_recovery_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtensionAssembly:
    """Objects owned by one AgentServer process for the lifetime of the runtime."""

    bus: ExtensionEventBus
    manager: ExtensionManager
    webui: WebUIRuntime
    transport: RenderTransport
    compact: SessionCompactionService
    authorization: SessionAuthorizationService


_ASSEMBLY: ExtensionAssembly | None = None
_LOCK = asyncio.Lock()
_LOCAL_CLI: Any = None


async def install_extensions(server: Any = None) -> ExtensionAssembly | None:
    """Assemble Extensions V2 once; a failure disables the feature, not the server."""

    global _ASSEMBLY, _LOCAL_CLI
    async with _LOCK:
        if _ASSEMBLY is not None:
            return _ASSEMBLY
        # Resolving extension paths reads config.yaml; the first read imports
        # openjiuwen.symphony. Keep that work off the event loop.
        try:
            install_dir = await asyncio.to_thread(resolve_install_dir)
            report_path = await asyncio.to_thread(resolve_install_report_path)
            settings = await asyncio.to_thread(resolve_install_report_settings)
            store_dir = await asyncio.to_thread(resolve_store_dir)
            state_dir = await asyncio.to_thread(resolve_state_dir)
        except Exception:  # noqa: BLE001 - a broken install layout disables extensions, not the server
            logger.exception("[ExtensionsV2] extension assembly is unavailable")
            return None
        try:
            await asyncio.to_thread(
                begin_system_plugin_recovery_session,
                install_dir,
                report_path.parent,
            )
        except Exception:  # noqa: BLE001 - recovery history must not block extension startup
            logger.warning(
                "[ExtensionsV2] could not create system plugin recovery startup record",
                exc_info=True,
            )
        try:
            assembly = _build_assembly(
                server,
                install_dir=install_dir,
                report_path=report_path,
                settings=settings,
                store_dir=store_dir,
                state_dir=state_dir,
            )

            async def cleanup_package_views(package_id: str, generation: int, reason: str) -> None:
                # Shutdown clears views in webui.shutdown(); do not fail package
                # release because a single clear could not be delivered.
                if reason == "shutdown":
                    return
                await assembly.webui.drop_package(package_id, generation=generation)
                if reason == "reload":
                    await assembly.webui.notify_packages_changed([package_id])

            async def cleanup_package_authorization(package_id: str, generation: int, reason: str) -> None:
                del reason
                assembly.authorization.revoke_package(package_id, generation)

            await assembly.manager.init(
                assembly.bus,
                host_version=_host_version(),
                cleanup_callbacks=(cleanup_package_views, cleanup_package_authorization),
                host_event_types=lambda bus: bootstrap.register_event_types(bus, assembly.webui),
                host_handlers=lambda bus: bootstrap.register_handlers(
                    bus,
                    assembly.webui,
                    file_service=getattr(server, "session_workspace_files", None),
                    compact_service=assembly.compact,
                    authorization_service=assembly.authorization,
                ),
            )
            if assembly.manager.state_store is not None and assembly.manager.state_dependencies is not None:
                assembly.webui.bind_state(
                    assembly.manager.state_store,
                    assembly.manager.state_dependencies,
                )
        except Exception:  # noqa: BLE001 - extensions must not block chat startup
            logger.exception("[ExtensionsV2] assembly failed; extension features are unavailable")
            return None

        await _activate_installed(
            assembly.manager, install_dir=install_dir, report_path=report_path,
        )
        _wire_team_extension_bus(assembly.bus)
        try:
            _publish_extension_bus(assembly.bus)
        except Exception:  # noqa: BLE001 - bus publish must not block assembly registration
            logger.exception("[ExtensionsV2] failed to publish the AgentLoop extension bus")
        _ASSEMBLY = assembly
        try:
            from jiuwenswarm.extensions_v2.management.local_cli import start_local_cli

            _LOCAL_CLI = await start_local_cli()
        except Exception:  # noqa: BLE001 - CLI unavailability must not disable the host
            logger.exception("[ExtensionsV2] local CLI unavailable")
        logger.info("[ExtensionsV2] assembly ready")
        return assembly


def get_extension_manager() -> ExtensionManager | None:
    return _ASSEMBLY.manager if _ASSEMBLY is not None else None


def get_extension_bus() -> ExtensionEventBus | None:
    return _ASSEMBLY.bus if _ASSEMBLY is not None else None


def get_webui_runtime() -> WebUIRuntime | None:
    return _ASSEMBLY.webui if _ASSEMBLY is not None else None


def get_compaction_service() -> SessionCompactionService | None:
    return _ASSEMBLY.compact if _ASSEMBLY is not None else None


def get_authorization_service() -> SessionAuthorizationService | None:
    """Return the host binding service; no credential collector is installed."""
    return _ASSEMBLY.authorization if _ASSEMBLY is not None else None


async def shutdown_extensions() -> None:
    """Release active views and package handlers without deleting install copies."""

    global _ASSEMBLY, _LOCAL_CLI
    async with _LOCK:
        assembly = _ASSEMBLY
        if assembly is None:
            return
        if _LOCAL_CLI is not None:
            await _LOCAL_CLI.close()
            _LOCAL_CLI = None
        try:
            _publish_extension_bus(None)
        except Exception:  # noqa: BLE001 - withdrawing the bus must not block teardown
            logger.exception("[ExtensionsV2] failed to withdraw the AgentLoop extension bus")
        try:
            from jiuwenswarm.extensions_v2.management import containers

            await containers.cancel_retries()
        except Exception:  # noqa: BLE001 - retry cleanup must not block teardown
            logger.warning("[ExtensionsV2] failed to cancel package retries", exc_info=True)
        try:
            await assembly.compact.close()
            await assembly.webui.stop_state_refresh()
            await assembly.manager.shutdown()
        finally:
            assembly.authorization.close()
        await assembly.webui.shutdown()
        _ASSEMBLY = None


def _wire_team_extension_bus(bus: ExtensionEventBus) -> None:
    """Hand the bus to the team runtime manager so team builds go through it.

    Soft-gated like the bootstrap registrations: an agent-core without the
    team extension surface keeps the rest of the assembly alive.
    """

    try:
        from openjiuwen.core.runner import Runner
    except ImportError as exc:
        logger.warning("[ExtensionsV2] team bus wiring unavailable (agent-core too old): %s", exc)
        return
    Runner.set_team_extension_bus(bus)


def _publish_extension_bus(bus: ExtensionEventBus | None) -> None:
    """Publish (or withdraw with ``None``) the bus AgentLoop tool events go through.

    Soft-gated: an agent-core without ``Runner.set_extension_bus`` keeps
    AgentLoop on its direct path.
    """

    try:
        from openjiuwen.core.runner import Runner
    except ImportError as exc:
        logger.warning("[ExtensionsV2] AgentLoop bus publishing unavailable (agent-core too old): %s", exc)
        return

    set_extension_bus = getattr(Runner, "set_extension_bus", None)
    if set_extension_bus is None:
        logger.warning("[ExtensionsV2] AgentLoop bus publishing unavailable (agent-core too old)")
        return
    set_extension_bus(bus)


def _build_assembly(
    server: Any,
    *,
    install_dir: Path,
    report_path: Path,
    settings: InstallReportSettings,
    store_dir: Path,
    state_dir: Path,
) -> ExtensionAssembly:
    bus = SessionStartEventBus()
    manager = ExtensionManager(
        install_dir,
        report_path=report_path,
        poll_interval=settings.poll_interval,
        dependency_timeout=settings.dependency_timeout,
        lock_timeout=settings.lock_timeout,
        store_dir=store_dir,
        state_dir=state_dir,
    )
    transport = RenderTransport(push=_push_from(server))
    catalog = WebUICatalogService(manager.active_packages)
    webui = WebUIRuntime(
        bus=bus,
        transport=transport,
        catalog=catalog,
        workspace_locator=_workspace_locator(server),
    )
    compact = SessionCompactionService(
        bus, policy_epoch=lambda: tuple((record.package_id, id(record)) for record in manager.active_packages()),
        invocation_guard=lambda ops: manager.registry.is_ready and manager.registry.runtime.is_active_invocation(ops),
    )
    authorization = _build_authorization_service(bus, manager)
    return ExtensionAssembly(bus=bus, manager=manager, webui=webui, transport=transport,
                             compact=compact, authorization=authorization)


def _build_authorization_service(bus: ExtensionEventBus, manager: ExtensionManager) -> SessionAuthorizationService:
    def supported() -> bool:
        return (manager.registry.is_ready
                and callable(getattr(manager.registry.runtime, "is_active_invocation", None))
                and callable(getattr(manager.registry.runtime, "invocation_generation", None)))

    def live(caller: AuthorizationCaller) -> bool:
        if not manager.registry.is_ready:
            return False
        metadata = caller.metadata
        active = manager.registry.runtime.get_active(metadata.package_id)
        return bool(active is not None and active.generation == caller.generation
                    and active.version == metadata.package_version and active.root == metadata.root)

    def caller_for_ops(ops) -> AuthorizationCaller | None:
        if not supported() or not manager.registry.runtime.is_active_invocation(ops):
            return None
        metadata = ops.package_metadata
        if metadata is None:
            return None
        generation = manager.registry.runtime.invocation_generation(ops)
        if generation is None:
            return None
        caller = AuthorizationCaller(metadata, generation)
        return caller if live(caller) else None

    return SessionAuthorizationService(bus, caller_for_ops=caller_for_ops,
                                       caller_is_live=live, supported=supported)


async def _activate_installed(
    manager: ExtensionManager, *, install_dir: Path, report_path: Path,
) -> None:
    try:
        from jiuwenswarm.extensions_v2.management import containers

        containers.register_active_managed_roots(manager)
    except Exception:  # noqa: BLE001 - registration failure must not block standalone packages
        logger.exception("[ExtensionsV2] failed to register bundle member roots")
    try:
        await manager.load_installed()
    except Exception:  # noqa: BLE001 - one bad package must not disable the rest
        logger.exception("[ExtensionsV2] failed to activate installed packages")
    try:
        from jiuwenswarm.extensions_v2.management import containers

        await containers.recover_containers(manager)
    except Exception:  # noqa: BLE001 - recovery must not block chat startup
        logger.exception("[ExtensionsV2] failed to recover package containers")
    try:
        from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery

        def _settle_startup() -> None:
            SystemPluginRecovery(install_dir, report_path.parent).settle_startup()

        await asyncio.to_thread(_settle_startup)
    except Exception:  # noqa: BLE001 - recovery history must not block startup
        logger.warning("[ExtensionsV2] could not settle system plugin recovery startup record", exc_info=True)


def _push_from(server: Any) -> Any:
    return server if callable(getattr(server, "send_push", None)) else None


def _workspace_locator(server: Any) -> Any:
    locate = getattr(getattr(server, "session_workspace_files", None), "workspace", None)
    return locate if callable(locate) else None


def _host_version() -> str:
    return get_runtime_version()
