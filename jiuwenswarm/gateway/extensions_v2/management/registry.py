# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway facade for the shared extension install store.

Gateway packages live in the same ``extension_pkgs`` tree as AgentServer
packages; this facade only touches entries whose manifest says
``domain: gateway``. File layout, zip guards and copies come from agent-core's
``PackageManager``; publishing and releasing handlers stay on the Gateway
runtime, so a package never needs a second ``ops.build``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.errors import OperationError, operation_error
from openjiuwen.core.extension.package_manager import PackageManager

from jiuwenswarm.gateway.extensions_v2.loader import load_gateway_package
from jiuwenswarm.gateway.extensions_v2.manifest import DOMAIN_GATEWAY
from jiuwenswarm.gateway.extensions_v2.management.manifest_source import (
    InstalledPackage,
    list_installed,
    read_domain,
)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime

logger = logging.getLogger(__name__)

_DELETE_ATTEMPTS = 3
_DELETE_BACKOFF_SECONDS = 0.2


class GatewayPackageManager:
    """Install, reload and release Gateway packages in the shared store."""

    def __init__(
        self,
        runtime: GatewayRuntime,
        install_dir: Path,
        *,
        on_released: Callable[[str], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self.install_dir = Path(install_dir).expanduser()
        self._store = PackageManager(self.install_dir)
        self._lock = asyncio.Lock()
        self._on_released = on_released

    # ── queries (no locking: read-only snapshot) ──────────────────────────

    def installed(self) -> list[InstalledPackage]:
        """Every installed package in the shared store, both domains."""

        return list_installed(self.install_dir)

    def gateway_installed(self, package_id: str | None = None) -> list[InstalledPackage]:
        """Installed Gateway packages, optionally one package id."""

        return [
            package
            for package in self.installed()
            if package.domain == DOMAIN_GATEWAY
            and (package_id is None or package.package_id == package_id)
        ]

    def owns(self, package_id: str) -> bool:
        """True when ``package_id`` has a Gateway copy in the shared store."""

        return any(package.package_id == package_id for package in self.gateway_installed(package_id))

    def source_domain(self, path: Path) -> str | None:
        """Read the host declared by a zip or directory source; None when it is not a package."""

        try:
            with self._store.open_source(Path(path)) as opened:
                return read_domain(opened.root)
        except ExtensionFailure:
            return None

    def owns_source(self, path: Path) -> bool:
        """True when the source declares an installable Gateway package."""

        return self.source_domain(path) == DOMAIN_GATEWAY

    def query(self, package_id: str | None = None) -> dict[str, Any]:
        """Return Gateway package facts in the AgentServer query shape."""

        active = [
            package
            for package in self._runtime.active_packages()
            if package_id is None or package.package_id == package_id
        ]
        installed = self.gateway_installed(package_id)
        records = [
            {
                "package_id": package.package_id,
                "version": package.version,
                "root": str(package.root),
                "generation": 1,
                "requires_packages": [],
            }
            for package in active
        ]
        installed_packages = [
            {
                "package_id": package.package_id,
                "version": package.version,
                "install_path": str(package.root),
            }
            for package in installed
        ]
        return {
            "active": records,
            "installed": [str(package.root) for package in installed],
            "installed_packages": installed_packages,
            "inflight": {},
            "published": {package.package_id: [] for package in active},
        }

    # ── mutations ─────────────────────────────────────────────────────────

    async def install(self, paths: Sequence[Path]) -> list[ActivationReport]:
        """Copy each Gateway zip into the shared store and publish it."""

        async with self._lock:
            reports: list[ActivationReport] = []
            for path in paths:
                reports.append(self._install_one(Path(path)))
            return reports

    async def reload(self, package_id: str) -> ActivationReport:
        """Re-read the installed copy and republish its handlers."""

        async with self._lock:
            root = self._highest(package_id)
            if root is None:
                return _failure(
                    package_id,
                    "reload",
                    operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
                )
            try:
                self._runtime.publish(load_gateway_package(root))
            except Exception as exc:  # noqa: BLE001 - one bad package must surface as a report
                logger.exception("[GatewayExtensions] cannot reload %s", package_id)
                return _failure(
                    package_id,
                    "reload",
                    operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
                )
            return _success(package_id, "reload")

    async def uninstall(self, package_id: str) -> ActivationReport:
        """Release the handlers and delete every installed copy of one package."""

        async with self._lock:
            versions = self.gateway_installed(package_id)
            released = self._runtime.unpublish(package_id)
            if not versions and not released:
                return _failure(
                    package_id,
                    "uninstall",
                    operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
                )
            failure: OperationError | None = None
            for package in versions:
                try:
                    await _remove_installed(self._store, package)
                except ExtensionFailure as exc:
                    failure = exc.as_error()
                    logger.warning("[GatewayExtensions] cannot delete %s: %s", package.root, exc)
            if self._on_released is not None:
                try:
                    self._on_released(package_id)
                except Exception:  # noqa: BLE001 - view cleanup must not fail management
                    logger.exception("[GatewayExtensions] release cleanup failed for %s", package_id)
            if failure is not None:
                return _failure(package_id, "uninstall", failure)
            return _success(package_id, "uninstall")

    # ── internals ─────────────────────────────────────────────────────────

    def _install_one(self, path: Path) -> ActivationReport:
        package_id = ""
        try:
            with self._store.open_source(path) as opened:
                package_id = opened.manifest.package_id
                if read_domain(opened.root) != DOMAIN_GATEWAY:
                    return _failure(
                        package_id,
                        "install",
                        operation_error(
                            StatusCode.EXTENSION_SOURCE_INVALID,
                            reason=f"{package_id} is not a gateway package",
                        ),
                    )
                root = self._store.copy_into_store(opened.root, opened.manifest)
        except ExtensionFailure as exc:
            return _failure(package_id or path.stem, "install", exc.as_error())
        except Exception as exc:  # noqa: BLE001 - report, never raise to the frame
            logger.exception("[GatewayExtensions] cannot install %s", path)
            return _failure(
                package_id or path.stem,
                "install",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        try:
            self._runtime.publish(load_gateway_package(root))
        except Exception as exc:  # noqa: BLE001 - roll back the copy we just made
            logger.exception("[GatewayExtensions] cannot publish %s", package_id)
            try:
                self._store.remove_installed(package_id, root.name)
            except ExtensionFailure:
                logger.exception("[GatewayExtensions] cannot roll back %s", root)
            return _failure(
                package_id,
                "install",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        return _success(package_id, "install")

    def _highest(self, package_id: str) -> Path | None:
        versions = self.gateway_installed(package_id)
        if not versions:
            return None
        return max(versions, key=lambda package: _version_key(package.version)).root


def _success(package_id: str, operation: str) -> ActivationReport:
    return ActivationReport(
        plan_id="",
        package_id=package_id,
        operation=operation,
        status="SUCCESS",
        previous_generation=None,
        current_generation=1,
    )


async def _remove_installed(store: PackageManager, package: InstalledPackage) -> None:
    """Delete one copy, retrying transient file locks held by other processes."""

    for attempt in range(1, _DELETE_ATTEMPTS + 1):
        try:
            store.remove_installed(package.package_id, package.version)
            return
        except ExtensionFailure:
            if attempt == _DELETE_ATTEMPTS:
                raise
            await asyncio.sleep(_DELETE_BACKOFF_SECONDS * attempt)


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


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)
