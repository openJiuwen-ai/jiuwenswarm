# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm facade over the agent-core extension ``Registry``.

The facade keeps only host concerns: install-directory configuration, startup
selection, the inactive callback and shutdown. Package parsing, planning,
publication and active records stay in agent-core.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.events import Event, ExtensionEventBus
from openjiuwen.core.extension import (
    ActivePackageRecord,
    ActivationReport,
    ExtensionFailure,
    Registry,
)
from openjiuwen.core.extension.errors import OperationError
from openjiuwen.core.extension.manifest import load_manifest
from openjiuwen.core.extension.management import (
    PACKAGE_QUERY_EVENT,
    QueryRequest,
    QueryResult,
    unbind_context,
)
from openjiuwen.core.extension.versions import Version, parse_version

logger = logging.getLogger(__name__)

CODE_MANAGER_NOT_INITIALIZED = "EXTENSION_MANAGER_NOT_INITIALIZED"


class ExtensionManager:
    """Stable host entry; all control-plane work is delegated to ``Registry``."""

    def __init__(self, install_dir: Path) -> None:
        self.install_dir = Path(install_dir).expanduser().resolve()
        self._registry = Registry()
        self._bus: ExtensionEventBus | None = None
        self._inactive_callback: Callable[[str], Awaitable[None]] | None = None

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
    ) -> None:
        """Bind the bus and open the install directory once.

        ``Registry.init`` registers host events and handlers, then triggers the
        single ``ops.build``. Callers must not trigger it again.
        """

        if self._bus is not None:
            return
        try:
            await self._registry.init(
                event_bus,
                host_event_types=host_event_types,
                host_handlers=host_handlers,
            )
            self._registry.open_install_dir(self.install_dir, host_version=host_version)
        except Exception:
            if self._registry.is_ready:
                unbind_context(event_bus)
            self._registry = Registry()
            raise
        self._bus = event_bus

    async def install(self, paths: Sequence[Path]) -> list[ActivationReport]:
        """Install each source through ``package.install``."""

        self._require_ready()
        package_manager = self._registry.package_manager
        before = set(package_manager.list_installed())
        reports = list(await self._registry.install(list(paths)))
        self._remove_failed_copies(reports, set(package_manager.list_installed()) - before)
        return reports

    async def load_installed(self) -> list[ActivationReport]:
        """Activate the highest installed version of every inactive package."""

        self._require_ready()
        roots = self._pending_roots()
        if not roots:
            return []
        return list(await self._registry.install(roots))

    async def uninstall(self, package_id: str) -> ActivationReport:
        """Remove an active package, or delete an installed-only copy."""

        self._require_ready()
        report = await self._registry.uninstall(package_id)
        if _is_not_active(report):
            removed, error = self._remove_inactive(package_id)
            if removed:
                await self._notify_inactive(package_id)
                return _success(package_id, "uninstall")
            if error is not None:
                return _failure(package_id, "uninstall", error)
        if report.status == "SUCCESS":
            await self._notify_inactive(package_id)
        return report

    async def reload(self, package_id: str) -> ActivationReport:
        """Hot-switch an active package from its installed copy."""

        self._require_ready()
        return await self._registry.reload(package_id)

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

        if self._bus is None:
            return
        bus = self._bus
        runtime = self._registry.runtime
        active = {record.package_id: record for record in runtime.active_packages()}
        remaining = set(active)
        while remaining:
            ready = [
                package_id
                for package_id in sorted(remaining)
                if not any(
                    dependency.package_id in remaining
                    for dependency in active[package_id].requires_packages
                    if dependency.package_id != package_id
                )
            ]
            if not ready:
                ready = sorted(remaining)
            for package_id in ready:
                try:
                    runtime.unpublish(package_id, bus)
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    logger.exception("[ExtensionsV2] failed to release package %s", package_id)
                remaining.discard(package_id)
                await self._notify_inactive(package_id)
        self._bus = None
        self._registry = Registry()

    def _pending_roots(self) -> list[Path]:
        active = {record.package_id for record in self._registry.runtime.active_packages()}
        highest: dict[str, tuple[Version, Path]] = {}
        for root in self._registry.package_manager.list_installed():
            try:
                manifest = load_manifest(root)
                version = parse_version(manifest.version)
            except (ExtensionFailure, ValueError) as exc:
                logger.warning("[ExtensionsV2] skipping invalid installed package %s: %s", root, exc)
                continue
            if manifest.package_id in active:
                continue
            current = highest.get(manifest.package_id)
            if current is None or version > current[0]:
                highest[manifest.package_id] = (version, root)
        return [root for _, root in highest.values()]

    def _remove_inactive(self, package_id: str) -> tuple[bool, OperationError | None]:
        manager = self._registry.package_manager
        roots = [root for root in manager.list_installed() if root.parent.name == package_id]
        if not roots:
            return False, None
        for root in roots:
            try:
                manager.remove_installed(package_id, root.name)
            except ExtensionFailure as exc:
                return False, exc.as_error()
        return True, None

    def _remove_failed_copies(
        self,
        reports: Sequence[ActivationReport],
        created: set[Path],
    ) -> None:
        failed = {report.package_id for report in reports if report.status == "FAILED"}
        if not failed:
            return
        manager = self._registry.package_manager
        for root in sorted(created):
            if root.parent.name not in failed:
                continue
            if not root.is_dir():
                continue
            try:
                manager.remove_installed(root.parent.name, root.name)
            except ExtensionFailure:
                logger.exception("[ExtensionsV2] failed to remove rejected copy %s", root)

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


def _is_not_active(report: ActivationReport) -> bool:
    return (
        report.status == "FAILED"
        and report.error is not None
        and report.error.code == "EXTENSION_PACKAGE_NOT_INSTALLED"
    )


def _success(package_id: str, operation: str) -> ActivationReport:
    return ActivationReport(
        plan_id="",
        package_id=package_id,
        operation=operation,
        status="SUCCESS",
        previous_generation=None,
        current_generation=None,
    )


def _failure(package_id: str, operation: str, error: OperationError) -> ActivationReport:
    return ActivationReport(
        plan_id="",
        package_id=package_id,
        operation=operation,
        status="FAILED",
        previous_generation=None,
        current_generation=None,
        error=error,
    )
