# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm facade over the agent-core extension ``Registry``.

The facade keeps only host concerns: install-directory configuration, startup
report configuration, the inactive callback and shutdown. Package parsing, planning,
publication and active records stay in agent-core.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext
from openjiuwen.core.events.ops_impl.state import unconfigure_state
from openjiuwen.core.events.ops_impl.store import unconfigure_store
from openjiuwen.core.extension import (
    ActivePackageRecord,
    ActivationReport,
    ExtensionFailure,
    Manifest,
    PackageResourceCleanup,
    Registry,
)
from openjiuwen.core.extension.data import JsonState, JsonStore
from openjiuwen.core.extension.data.files import run_file_operation
from openjiuwen.core.extension.errors import OperationError, operation_error
from openjiuwen.core.extension.manifest import has_manifest, load_manifest
from openjiuwen.core.extension.management import (
    PACKAGE_QUERY_EVENT,
    QueryRequest,
    QueryResult,
    context_for,
    unbind_context,
)
from openjiuwen.core.extension.management.handlers import (
    _deactivate_keep_copy,
    _disable_installed_package,
    _enable_installed_package,
)

from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartOutput
from jiuwenswarm.extensions_v2.event_types.session_surface import RenderSurface
from jiuwenswarm.extensions_v2.management.session_start import SessionStartGate
from jiuwenswarm.extensions_v2.manifest_io import DOMAIN_AGENT_SERVER, DOMAIN_GATEWAY

logger = logging.getLogger(__name__)

CODE_MANAGER_NOT_INITIALIZED = "EXTENSION_MANAGER_NOT_INITIALIZED"


class ExtensionManager:
    """Stable host entry; all control-plane work is delegated to ``Registry``."""

    def __init__(
        self,
        install_dir: Path,
        *,
        report_path: Path,
        poll_interval: float = 0.5,
        dependency_timeout: float = 300.0,
        lock_timeout: float = 5.0,
        store_dir: Path | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.install_dir = Path(install_dir).expanduser().resolve()
        self.report_path = Path(report_path).expanduser().resolve()
        self._poll_interval = poll_interval
        self._dependency_timeout = dependency_timeout
        self._lock_timeout = lock_timeout
        self._store = JsonStore(store_dir) if store_dir is not None else None
        self._states = JsonState(state_dir) if state_dir is not None else None
        self._registry = Registry()
        self._operation_lock = asyncio.Lock()
        self._bus: ExtensionEventBus | None = None
        self._inactive_callback: Callable[[str], Awaitable[None]] | None = None
        self._session_start: SessionStartGate | None = None

    @property
    def initialized(self) -> bool:
        return self._bus is not None

    @property
    def registry(self) -> Registry:
        return self._registry

    async def init(
        self,
        event_bus: ExtensionEventBus,
        *,
        host_version: str | None = None,
        host_event_types: Callable[[ExtensionEventBus], None] | None = None,
        host_handlers: Callable[[ExtensionEventBus], None] | None = None,
        cleanup_callbacks: Sequence[PackageResourceCleanup] = (),
    ) -> None:
        """Bind the bus and open the install directory once.

        ``Registry.init`` registers host events and handlers, then triggers the
        single ``ops.build``. Callers must not trigger it again.
        """

        async with self._operation_lock:
            if self._bus is not None:
                return
            try:
                await self._registry.init(
                    event_bus,
                    host_event_types=host_event_types,
                    host_handlers=host_handlers,
                    store=self._store,
                    states=self._states,
                    cleanup_callbacks=cleanup_callbacks,
                )
                await self._registry.open_install_dir(
                    self.install_dir, report_path=self.report_path, host_version=host_version,
                    domain=DOMAIN_AGENT_SERVER, allowed_domains=(DOMAIN_GATEWAY, DOMAIN_AGENT_SERVER),
                    poll_interval=self._poll_interval, dependency_timeout=self._dependency_timeout,
                    lock_timeout=self._lock_timeout,
                )
            except asyncio.CancelledError:
                unconfigure_state(event_bus)
                unconfigure_store(event_bus)
                unbind_context(event_bus)
                self._registry = Registry()
                raise
            except Exception:
                if self._registry.is_ready:
                    unconfigure_state(event_bus)
                    unconfigure_store(event_bus)
                    unbind_context(event_bus)
                self._registry = Registry()
                raise
            self._bus = event_bus
            self._session_start = SessionStartGate(event_bus, self._registry.runtime)

    async def ensure_session_started(
        self,
        context: SessionContext,
        *,
        request_id: str,
        channel_id: str,
        is_interactive: bool,
        surface: RenderSurface | None = None,
    ) -> tuple[SessionStartOutput, ...]:
        """Initialize current Worker activations without holding management locks."""
        self._require_ready()
        gate = self._session_start
        if gate is None:
            raise RuntimeError("session.start gate is not initialized")
        return await gate.ensure_started(context, request_id=request_id, channel_id=channel_id,
                                         is_interactive=is_interactive, surface=surface)

    async def install(self, paths: Sequence[Path]) -> list[ActivationReport]:
        """Install each source; a different version of an active id switches to it."""
        self._require_ready()
        reports = await self._install_sources([Path(path) for path in paths])
        _record_recovery_change(
            self.install_dir,
            self.report_path.parent,
            "install",
            reports,
        )
        return reports

    async def _install_sources(self, sources: Sequence[Path]) -> list[ActivationReport]:
        """Keep every stored version and activate the uploaded one."""
        opened = []
        reports: list[ActivationReport] = []
        switches: list[tuple[Manifest, Path]] = []
        regular: list[Path] = []
        try:
            for source in sources:
                try:
                    item = await asyncio.to_thread(
                        self._registry.package_manager.open_source, source
                    )
                except ExtensionFailure as exc:
                    reports.append(_failure(str(source), exc.as_error()))
                    continue
                opened.append(item)

            counts: dict[str, int] = {}
            for item in opened:
                package_id = item.manifest.package_id
                counts[package_id] = counts.get(package_id, 0) + 1
            for item in opened:
                manifest = item.manifest
                if counts[manifest.package_id] > 1:
                    regular.append(item.root)
                    continue
                active = self._registry.runtime.get_active(manifest.package_id)
                if active is not None and active.version == manifest.version:
                    reports.append(
                        _failure(
                            manifest.package_id,
                            operation_error(
                                StatusCode.EXTENSION_PACKAGE_EXISTS,
                                package_id=manifest.package_id,
                                display_name=manifest.display_name or manifest.package_id,
                            ),
                        )
                    )
                    continue
                if (self.install_dir / manifest.package_id / manifest.version).is_dir():
                    # Keep the stored copy; Core refuses to overwrite it.
                    regular.append(item.root)
                    continue
                if active is not None and active.version != manifest.version and manifest.enable:
                    switches.append((manifest, item.root))
                else:
                    regular.append(item.root)
            if regular or not switches:
                reports.extend(await self._registry.install(regular))
            for manifest, root in switches:
                reports.append(
                    await self.enable_version(
                        manifest.package_id,
                        manifest.version,
                        source=root,
                    )
                )
            return reports
        finally:
            for item in opened:
                item.close()

    async def load_installed(self) -> list[ActivationReport]:
        """Pass only the configured root; Core owns discovery and stable ordering."""
        self._require_ready()
        return list(await self._registry.load_installed())

    async def uninstall(self, package_id: str, version: str = "") -> ActivationReport:
        """Remove one stored version, or every version when ``version`` is empty."""

        async with self._operation_lock:
            self._require_ready()
            was_active = self._registry.runtime.get_active(package_id) is not None
            if version:
                report = await self._uninstall_version(package_id, version)
            else:
                report = await self._registry.uninstall(package_id)
        if report.status == "SUCCESS":
            if was_active and self._registry.runtime.get_active(package_id) is None:
                await self._notify_inactive(package_id)
            _record_recovery_change(
                self.install_dir,
                self.report_path.parent,
                "uninstall",
                (report,),
            )
        return report

    async def _uninstall_version(self, package_id: str, version: str) -> ActivationReport:
        """Delete one version copy while every other stored version keeps its files."""

        manager = self._registry.package_manager
        root = manager.version_dir(package_id, version)
        if not root.is_dir() or not has_manifest(root):
            return _failure(
                package_id,
                operation_error(
                    StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED,
                    package_id=package_id,
                    reason=f"install store has no {package_id} {version}",
                ),
            )
        manifest = load_manifest(root, require_domain=False)
        context = context_for(self._bus)
        service = context.install_report
        token = None
        report = None
        try:
            token = await service.begin_operation(manifest, "uninstall")
            await service.validate_operation(token, manifest, "uninstall")
            active = context.runtime.get_active(package_id)
            with service.operation_scope(token):
                if active is not None and active.version == version:
                    report = await _deactivate_keep_copy(context, self._bus, manifest)
                if report is None or report.status == "SUCCESS":
                    await run_file_operation(
                        lambda: manager.remove_installed(package_id, version, missing_ok=True)
                    )
            if report is None:
                report = ActivationReport(
                    plan_id="",
                    package_id=package_id,
                    operation="uninstall",
                    status="SUCCESS",
                    previous_generation=None,
                    current_generation=None,
                )
        except ExtensionFailure as exc:
            report = _failure(package_id, exc.as_error())
        finally:
            if token is not None:
                await service.finish_operation(
                    token,
                    report=report,
                    active=context.runtime.get_active(package_id),
                    cleanup_pending=False,
                )
        if report is None:  # pragma: no cover - begin_operation always reports one outcome
            raise ExtensionFailure(StatusCode.EXTENSION_LOAD_FAILED, reason="uninstall produced no report")
        return report

    async def reload(self, package_id: str) -> ActivationReport:
        """Hot-switch an active package from its installed copy."""

        self._require_ready()
        return await self._registry.reload(package_id)

    async def enable_version(
        self,
        package_id: str,
        version: str,
        *,
        source: Path | None = None,
    ) -> ActivationReport:
        """Load one version and keep every other installed version on disk, unloaded.

        ``source`` is the package to install when the target version is not in the
        store yet; a stored version is re-activated in place. The target is
        persisted as enabled and the previously active version as disabled.
        """

        self._require_ready()
        store = self._registry.package_manager
        active = self._registry.runtime.get_active(package_id)
        if active is not None and active.version == version and source is None:
            return ActivationReport(
                plan_id="",
                package_id=package_id,
                operation="enable",
                status="SUCCESS",
                previous_generation=active.generation,
                current_generation=active.generation,
            )
        previous = active.version if active is not None else ""
        if active is not None:
            context = context_for(self._bus)
            report = await _disable_installed_package(
                context,
                self._bus,
                load_manifest(active.root, require_domain=False),
            )
            if report.status != "SUCCESS":
                return report
        stored = store.version_dir(package_id, version)
        if stored.is_dir():
            context = context_for(self._bus)
            report = await _enable_installed_package(
                context,
                self._bus,
                load_manifest(stored, require_domain=False),
                stored,
            )
        else:
            if source is None:
                raise ExtensionFailure(
                    StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED,
                    package_id=package_id,
                    reason=f"install store has no {package_id} {version}",
                )
            reports = list(await self.install([source]))
            report = reports[0] if reports else None
            if (
                report is not None
                and report.status == "SUCCESS"
                and self._registry.runtime.get_active(package_id) is None
                and stored.is_dir()
            ):
                context = context_for(self._bus)
                report = await _enable_installed_package(
                    context,
                    self._bus,
                    load_manifest(stored, require_domain=False),
                    stored,
                )
        if report is None or report.status != "SUCCESS":
            if previous:
                await self._restore_version(package_id, previous)
            return report or ActivationReport(
                plan_id="",
                package_id=package_id,
                operation="enable",
                status="FAILED",
                previous_generation=None,
                current_generation=None,
                error=operation_error(
                    StatusCode.EXTENSION_LOAD_FAILED,
                    package_id=package_id,
                    reason="package did not start",
                ),
            )
        return report

    async def _restore_version(self, package_id: str, version: str) -> None:
        """Best-effort return to the version that was active before a failed switch."""

        root = self._registry.package_manager.version_dir(package_id, version)
        try:
            if not root.is_dir():
                return
            await _enable_installed_package(
                context_for(self._bus),
                self._bus,
                load_manifest(root, require_domain=False),
                root,
            )
        except Exception:  # noqa: BLE001 - keep the original switch failure
            logger.warning("[ExtensionsV2] failed to restore %s@%s", package_id, version, exc_info=True)

    async def set_enabled(self, package_id: str, enabled: bool) -> ActivationReport:
        """Keep an installed package while changing whether its handlers are active."""

        async with self._operation_lock:
            self._require_ready()
            report = await self._registry.set_enabled(package_id, enabled)
        if report.status == "SUCCESS" and not enabled:
            await self._notify_inactive(package_id)
        if report.status == "SUCCESS":
            _record_recovery_change(
                self.install_dir,
                self.report_path.parent,
                "enable" if enabled else "disable",
                (report,),
            )
        return report

    async def query(self, package_id: str | None = None) -> QueryResult:
        """Return the facts agent-core can confirm for this process."""

        self._require_ready()
        result = await self._bus.trigger(
            Event(PACKAGE_QUERY_EVENT, QueryRequest(bus=self._bus, package_id=package_id))
        )
        if not isinstance(result, QueryResult):
            raise ExtensionFailure(
                StatusCode.EXTENSION_LOAD_FAILED,
                reason="package.query returned no query result",
            )
        return result

    def active_packages(self) -> tuple[ActivePackageRecord, ...]:
        """Return active package facts for consumers such as the UI catalog."""

        if self._bus is None:
            return ()
        return self._registry.runtime.active_packages()

    def set_inactive_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback invoked after a package stops being active."""

        self._inactive_callback = callback

    async def shutdown(self) -> None:
        """Release active handlers without deleting install copies."""

        notifications: list[str] = []
        failed: list[str] = []
        async with self._operation_lock:
            if self._bus is None:
                return
            bus = self._bus
            await self._registry.begin_shutdown()
            runtime = self._registry.runtime
            active = {record.package_id: record for record in runtime.active_packages()}
            remaining = set(active)
            while remaining:
                ready = [
                    package_id
                    for package_id in sorted(remaining)
                    if not any(
                        dependency.package_id == package_id
                        for dependent_id in remaining
                        if dependent_id != package_id
                        for dependency in active[dependent_id].requires_packages
                    )
                ]
                if not ready:
                    ready = sorted(remaining)
                for package_id in ready:
                    try:
                        decision = runtime.uninstall_decision(package_id)
                        if decision is not None:
                            logger.warning("[ExtensionsV2] cannot release %s: %s", package_id, decision.code)
                            failed.append(package_id)
                            continue
                        runtime.unpublish(package_id, bus, keep_record=True)
                        await runtime.cleanup_package_resources(
                            package_id, active[package_id].generation, reason="shutdown",
                        )
                        runtime.drop_active(package_id)
                    except Exception:  # noqa: BLE001 - continue releasing independent packages
                        logger.exception("[ExtensionsV2] failed to release package %s", package_id)
                        failed.append(package_id)
                    else:
                        notifications.append(package_id)
                    finally:
                        remaining.discard(package_id)
            if not failed:
                await self._registry.close()
                unconfigure_state(bus)
                unconfigure_store(bus)
                unbind_context(bus)
                self._bus = None
                self._session_start = None
                self._registry = Registry()

        for package_id in notifications:
            await self._notify_inactive(package_id)
        if failed:
            raise ExtensionFailure(
                StatusCode.EXTENSION_LOAD_FAILED,
                reason=f"shutdown could not release packages: {', '.join(failed)}",
            )

    async def _notify_inactive(self, package_id: str) -> None:
        callback = self._inactive_callback
        if callback is None:
            return
        try:
            await callback(package_id)
        except Exception:  # noqa: BLE001 - view cleanup must not fail management
            logger.exception("[ExtensionsV2] inactive callback failed for %s", package_id)

    def _require_ready(self) -> None:
        if self._bus is None:
            raise ExtensionFailure(
                StatusCode.EXTENSION_LOAD_FAILED,
                reason="extension manager is not initialized",
            )


def _failure(package_id: str, error: OperationError) -> ActivationReport:
    return ActivationReport(
        plan_id="",
        package_id=package_id,
        operation="install",
        status="FAILED",
        previous_generation=None,
        current_generation=None,
        error=error,
    )


def _record_recovery_change(
    install_dir: Path,
    data_dir: Path,
    action: str,
    reports: Sequence[ActivationReport],
) -> None:
    """Persist successful state transitions without changing their result."""
    package_ids = [report.package_id for report in reports if report.status == "SUCCESS"]
    if not package_ids:
        return
    try:
        SystemPluginRecovery(install_dir, data_dir).record(action, package_ids)
    except Exception:  # noqa: BLE001 - recovery history must not break a successful operation
        logger.warning("[ExtensionsV2] could not record system plugin recovery history", exc_info=True)
