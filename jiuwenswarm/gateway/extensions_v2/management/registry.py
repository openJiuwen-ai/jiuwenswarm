# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway facade for the shared extension install store.

Gateway packages live in the same ``extension_pkgs`` tree as AgentServer
packages; this facade only touches entries whose manifest says
``package.domain: gateway``. Discovery, version selection and ordering come
from agent-core's shared install report; publishing and releasing handlers stay
on the Gateway runtime, so a package never needs a second ``ops.build``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.data.files import run_file_operation
from openjiuwen.core.extension.errors import OperationError, operation_error
from openjiuwen.core.extension.install_report import PackageInstallReport, PackageOperationToken
from openjiuwen.core.extension.manifest import has_manifest, load_manifest
from openjiuwen.core.extension.package_manager import PackageManager
from openjiuwen.core.extension.schema.manifest import Manifest
from openjiuwen.core.extension.schema.records import ActivePackageRecord
from openjiuwen.core.extension.selection import evaluate_reload, resolve_install_set

from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery
from jiuwenswarm.gateway.extensions_v2.loader import load_gateway_package
from jiuwenswarm.gateway.extensions_v2.manifest import DOMAIN_AGENT_SERVER, DOMAIN_GATEWAY

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime

logger = logging.getLogger(__name__)

_DELETE_ATTEMPTS = 3
_DELETE_BACKOFF_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    """The host-facing fields of one installed version directory."""

    package_id: str
    version: str
    domain: str
    root: Path


class GatewayPackageManager:
    """Install, reload and release Gateway packages in the shared store."""

    def __init__(
        self,
        runtime: GatewayRuntime,
        install_dir: Path,
        *,
        report_path: Path | None = None,
        poll_interval: float = 0.5,
        dependency_timeout: float = 300.0,
        lock_timeout: float = 5.0,
        on_released: Callable[[str], None] | None = None,
        recovery_data_dir: Path | None = None,
    ) -> None:
        self._runtime = runtime
        self.install_dir = Path(install_dir).expanduser()
        # Same domain whitelist as AgentServer: the shared startup plan must
        # discover both hosts, and packages with a missing or unknown
        # ``package.domain`` are rejected instead of entering the shared order.
        # Gateway ownership is checked explicitly before any mutation.
        self._store = PackageManager(self.install_dir, allowed_domains=(DOMAIN_GATEWAY, DOMAIN_AGENT_SERVER))
        # Unconstrained reader for ``source_domain``: callers decide what to do with foreign domains.
        self._inspection = PackageManager(self.install_dir)
        self._report = (
            PackageInstallReport(
                Path(report_path),
                poll_interval=poll_interval,
                dependency_timeout=dependency_timeout,
                lock_timeout=lock_timeout,
            )
            if report_path is not None
            else None
        )
        if self._report is not None:
            self._store.install_report = self._report
        self._lock = asyncio.Lock()
        self._on_released = on_released
        if recovery_data_dir is None:
            recovery_data_dir = runtime.config.recovery_data_dir
        self._recovery_data_dir = recovery_data_dir
        self._inited = False
        self._stopping = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def init(self, *, host_version: str | None = None) -> None:
        """Open the shared report once for this process."""

        del host_version
        if self._inited or self._report is None:
            self._inited = True
            return
        await self._report.open(self._store.store, domain=DOMAIN_GATEWAY)
        self._inited = True
        await self._report.reconcile_packages(self._runtime.active_package_records())

    async def load_installed(self) -> list[ActivationReport]:
        """Start the Gateway packages selected by the shared startup plan."""

        if self._report is None:
            return []
        plan = await self._report.prepare_startup(self._store)
        reports: list[ActivationReport] = []
        for manifest in plan.ordered:
            if manifest.domain != DOMAIN_GATEWAY:
                continue
            root = self._store.version_dir(manifest.package_id, manifest.version)
            if not has_manifest(root):
                root = self._find_root(manifest.package_id, manifest.version) or root
            reports.append(await self._install_manifest(manifest, root, startup=True))
        await self._report.mark_started()
        return reports

    async def begin_shutdown(self) -> None:
        """Reject new operations before the runtime releases handlers."""

        if self._stopping:
            return
        self._stopping = True
        if self._report is not None:
            await self._report.mark_stopping()

    async def close(self) -> None:
        """Release the process lock held by the shared report."""

        if self._report is None:
            return
        await self._report.close()

    # ── queries (no locking: read-only snapshot) ──────────────────────────

    def installed(self) -> list[InstalledPackage]:
        """Every installed package in the shared store, both domains."""

        found: list[InstalledPackage] = []
        for root in self._store.list_installed():
            try:
                manifest = load_manifest(root, require_domain=False)
            except ExtensionFailure:
                continue
            found.append(
                InstalledPackage(
                    manifest.package_id,
                    manifest.version,
                    _domain_of(manifest),
                    root,
                )
            )
        return found

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

        return bool(self.gateway_installed(package_id)) or self._runtime.active_record(package_id) is not None

    def source_domain(self, path: Path) -> str | None:
        """Read the host declared by a zip or directory source; None when invalid."""

        try:
            with self._inspection.open_source(Path(path)) as opened:
                return _domain_of(opened.manifest)
        except ExtensionFailure:
            return None

    def owns_source(self, path: Path) -> bool:
        """True when the source declares an installable Gateway package."""

        return self.source_domain(path) == DOMAIN_GATEWAY

    async def get_source_domain(self, path: Path) -> str:
        """Declared host of a source; invalid sources raise instead of guessing."""

        def read() -> str:
            with self._inspection.open_source(Path(path)) as opened:
                return _domain_of(opened.manifest)

        return await asyncio.to_thread(read)

    async def get_package_domain(self, package_id: str) -> str | None:
        """Host that owns ``package_id``, preferring the shared report."""

        if self._report is not None:
            states = await self._report.read_packages(package_id)
            state = states.get(package_id)
            if state is not None and state.domain:
                return state.domain.strip().lower()
        for package in self.installed():
            if package.package_id == package_id:
                return package.domain
        if self._runtime.active_record(package_id) is not None:
            return DOMAIN_GATEWAY
        return None

    def query(self, package_id: str | None = None) -> dict[str, Any]:
        """Return Gateway package facts in the AgentServer query shape."""

        active = [
            record
            for record in self._runtime.active_package_records()
            if package_id is None or record.package_id == package_id
        ]
        installed = self.gateway_installed(package_id)
        records = [
            {
                "package_id": record.package_id,
                "version": record.version,
                "root": str(record.root),
                "generation": record.generation,
                "requires_packages": [
                    {"package_id": item.package_id, "version": item.version}
                    for item in record.requires_packages
                ],
                "domain": record.domain,
            }
            for record in active
        ]
        installed_packages = [
            {
                "package_id": package.package_id,
                "version": package.version,
                "domain": package.domain,
                "install_path": str(package.root),
            }
            for package in installed
        ]
        return {
            "active": records,
            "package_ids": [record["package_id"] for record in records],
            "installed": [str(package.root) for package in installed],
            "installed_packages": installed_packages,
            "inflight": {
                record.package_id: self._runtime.inflight_count(record.package_id)
                for record in active
                if self._runtime.inflight_count(record.package_id)
            },
            "published": {
                record.package_id: self._runtime.published_handlers(record.package_id)
                for record in active
            },
        }

    # ── mutations ─────────────────────────────────────────────────────────

    async def install(self, paths: Sequence[Path]) -> list[ActivationReport]:
        """Copy each Gateway source into the shared store and publish it."""

        async with self._lock:
            if self._report is None:
                reports = [await self._legacy_install_path(Path(path)) for path in paths]
            else:
                reports = await self._install_batch(paths)
        _record_recovery_change(
            self.install_dir,
            self._recovery_data_dir,
            "install",
            reports,
        )
        return reports

    async def reload(self, package_id: str) -> ActivationReport:
        """Re-read the installed copy and republish its handlers."""

        async with self._lock:
            if self._report is None:
                return await self._legacy_reload(package_id)
            return await self._reload_reported(package_id)

    async def set_enabled(self, package_id: str, enabled: bool) -> ActivationReport:
        """Keep an installed Gateway package while changing its loading state."""

        async with self._lock:
            root = self._highest(package_id)
            if root is None:
                return _failure(
                    package_id,
                    "enable" if enabled else "disable",
                    operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
                )
            try:
                manifest = load_manifest(root, require_domain=True)
                if manifest.domain != DOMAIN_GATEWAY:
                    raise ExtensionFailure(StatusCode.EXTENSION_DOMAIN_MISMATCH, package_id=package_id)
                if enabled:
                    report = await self._enable(root, manifest)
                else:
                    report = await self._disable(root, manifest)
            except ExtensionFailure as exc:
                report = _failure(package_id, "enable" if enabled else "disable", exc.as_error())
            except Exception as exc:  # noqa: BLE001 - report malformed or failed Gateway loads
                logger.exception("[GatewayExtensions] cannot change enabled state for %s", package_id)
                report = _failure(
                    package_id,
                    "enable" if enabled else "disable",
                    operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
                )
        if report.status == "SUCCESS":
            _record_recovery_change(
                self.install_dir,
                self._recovery_data_dir,
                "enable" if enabled else "disable",
                (report,),
            )
        return report

    async def uninstall(self, package_id: str) -> ActivationReport:
        """Release the handlers and delete every installed copy of one package.

        Refused with ``EXTENSION_PACKAGE_BUSY`` while a handler or data call of
        the package is in flight, so data is never deleted under a running call.
        """

        async with self._lock:
            if self._report is None:
                report = await self._legacy_uninstall(package_id)
            else:
                report = await self._uninstall_reported(package_id)
        if report.status == "SUCCESS":
            _record_recovery_change(
                self.install_dir,
                self._recovery_data_dir,
                "uninstall",
                (report,),
            )
        return report

    # ── shared-report install ─────────────────────────────────────────────

    async def _install_batch(self, paths: Sequence[Path]) -> list[ActivationReport]:
        assert self._report is not None
        reports: list[ActivationReport] = []
        opened_sources = []
        parsed: list[tuple[Manifest, Path]] = []
        try:
            for path in paths:
                source = Path(path)
                try:
                    opened = await asyncio.to_thread(self._inspection.open_source, source)
                except ExtensionFailure as exc:
                    reports.append(_failure(source.stem, "install", exc.as_error()))
                    continue
                if _domain_of(opened.manifest) != DOMAIN_GATEWAY:
                    reports.append(
                        _failure(
                            opened.manifest.package_id,
                            "install",
                            operation_error(
                                StatusCode.EXTENSION_DOMAIN_MISMATCH,
                                reason=f"package belongs to {_domain_of(opened.manifest)!r}",
                            ),
                        )
                    )
                    opened.close()
                    continue
                opened_sources.append(opened)
                parsed.append((opened.manifest, opened.root))

            if not parsed:
                return reports

            states = tuple((await self._report.read_packages()).values())
            resolved = resolve_install_set(
                [manifest for manifest, _ in parsed],
                self._runtime.active_package_records(),
                _host_version(),
                external_packages=states,
            )
            if resolved.fatal is not None:
                for manifest, _ in parsed:
                    await self._reject(manifest, resolved.fatal)
                    reports.append(_failure(manifest.package_id, "install", resolved.fatal))
                return reports

            sources = {manifest.package_id: root for manifest, root in parsed}
            failed: set[str] = set()
            for rejection in resolved.rejected:
                manifest = next(item for item, _ in parsed if item.package_id == rejection.package_id)
                await self._reject(manifest, rejection.error)
                failed.add(rejection.package_id)
                reports.append(_failure(rejection.package_id, "install", rejection.error))

            for manifest in resolved.ordered:
                blocked = sorted(
                    item.package_id for item in manifest.requires_packages if item.package_id in failed
                )
                if blocked:
                    failed.add(manifest.package_id)
                    error = operation_error(
                        StatusCode.EXTENSION_DEPENDENCY_NOT_MET,
                        package_id=manifest.package_id,
                        reason=f"dependency {', '.join(blocked)} failed in this batch",
                    )
                    await self._reject(manifest, error)
                    reports.append(_failure(manifest.package_id, "install", error))
                    continue
                report = await self._install_manifest(manifest, sources[manifest.package_id])
                if report.status != "SUCCESS":
                    failed.add(manifest.package_id)
                reports.append(report)
            return reports
        finally:
            for opened in opened_sources:
                opened.close()

    async def _install_manifest(
        self, manifest: Manifest, source_root: Path, *, startup: bool = False,
    ) -> ActivationReport:
        assert self._report is not None
        service = self._report
        token: PackageOperationToken | None = None
        report: ActivationReport | None = None
        created = False
        cleanup_pending = False
        try:
            if _domain_of(manifest) != DOMAIN_GATEWAY:
                raise ExtensionFailure(
                    StatusCode.EXTENSION_DOMAIN_MISMATCH,
                    reason=f"package {manifest.package_id} belongs to {_domain_of(manifest)!r}",
                )
            self._store.validate_manifest(manifest, match_domain=False)
            token = await service.begin_operation(manifest, "install", startup=startup)
            await service.wait_for_dependencies(token)
            await service.validate_operation(token, manifest, "install")
            with service.operation_scope(token):
                root = await run_file_operation(
                    lambda: self._store.copy_into_store(source_root, manifest)
                )
            created = root != Path(source_root).resolve()
            report = await self._publish_manifest(manifest, root, "install")
            if report.status != "SUCCESS" and created and self._runtime.active_record(
                manifest.package_id
            ) is None:
                with service.operation_scope(token):
                    try:
                        await run_file_operation(
                            lambda: self._store.remove_installed(
                                manifest.package_id, manifest.version, missing_ok=True
                            )
                        )
                    except ExtensionFailure:
                        cleanup_pending = True
        except ExtensionFailure as exc:
            report = _failure(manifest.package_id, "install", exc.as_error())
        except Exception as exc:  # noqa: BLE001 - a local failure is a report, not a crash
            logger.exception("[GatewayExtensions] cannot install %s", manifest.package_id)
            report = _failure(
                manifest.package_id,
                "install",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        finally:
            if token is not None:
                await _finish_operation(
                    service,
                    token,
                    report=report,
                    active=self._runtime.active_record(manifest.package_id),
                    cleanup_pending=cleanup_pending,
                )
        return report or _failure(
            manifest.package_id,
            "install",
            operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason="package did not start"),
        )

    async def _reload_reported(self, package_id: str) -> ActivationReport:
        assert self._report is not None
        service = self._report
        record = self._runtime.active_record(package_id)
        if record is None:
            return _failure(
                package_id,
                "reload",
                operation_error(
                    StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED,
                    package_id=package_id,
                    reason="package is not active; cannot reload",
                ),
            )
        root = self._store.version_dir(package_id, record.version)
        if not has_manifest(root):
            root = record.root
        try:
            manifest = load_manifest(root, require_domain=False)
        except ExtensionFailure as exc:
            return _failure(package_id, "reload", exc.as_error())
        if _domain_of(manifest) != DOMAIN_GATEWAY:
            return _failure(
                package_id,
                "reload",
                operation_error(
                    StatusCode.EXTENSION_MANIFEST_INVALID,
                    reason="package.domain must be gateway",
                ),
            )

        states = tuple((await service.read_packages()).values())
        error = evaluate_reload(
            self._runtime.active_package_records(),
            package_id,
            manifest,
            external_packages=states,
        )
        if error is not None:
            return _failure(package_id, "reload", error)

        token: PackageOperationToken | None = None
        report: ActivationReport | None = None
        try:
            token = await service.begin_operation(manifest, "reload")
            await service.wait_for_dependencies(token)
            await service.validate_operation(token, manifest, "reload")
            report = await self._publish_manifest(manifest, root, "reload")
        except ExtensionFailure as exc:
            report = _failure(package_id, "reload", exc.as_error())
        except Exception as exc:  # noqa: BLE001 - a local failure is a report, not a crash
            logger.exception("[GatewayExtensions] cannot reload %s", package_id)
            report = _failure(
                package_id,
                "reload",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        finally:
            if token is not None:
                await _finish_operation(
                    service,
                    token,
                    report=report,
                    active=self._runtime.active_record(package_id),
                    cleanup_pending=False,
                )
        return report or _failure(
            package_id,
            "reload",
            operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason="package did not start"),
        )

    async def _uninstall_reported(self, package_id: str) -> ActivationReport:
        assert self._report is not None
        service = self._report
        record = self._runtime.active_record(package_id)
        try:
            versions = self._store.managed_versions(
                package_id,
                known_versions=tuple(item.version for item in self.gateway_installed(package_id)),
            )
        except ExtensionFailure as exc:
            return _failure(package_id, "uninstall", exc.as_error())
        if record is None and not versions:
            return _failure(
                package_id,
                "uninstall",
                operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
            )
        if self._runtime.inflight_count(package_id) > 0:
            return _failure(
                package_id,
                "uninstall",
                operation_error(
                    StatusCode.EXTENSION_PACKAGE_BUSY,
                    package_id=package_id,
                    reason="package handlers are still running",
                ),
            )

        manifest = self._manifest_for(package_id, record, versions)
        if manifest is None:
            return _failure(
                package_id,
                "uninstall",
                operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
            )

        token: PackageOperationToken | None = None
        try:
            token = await service.begin_operation(manifest, "uninstall")
            await service.validate_operation(token, manifest, "uninstall")
        except ExtensionFailure as exc:
            # Nothing changed yet: the copies stay and the token is released as failed.
            report = _failure(package_id, "uninstall", exc.as_error())
            if token is not None:
                await _finish_operation(
                    service,
                    token,
                    report=report,
                    active=record,
                    cleanup_pending=bool(self.gateway_installed(package_id)),
                )
            return report
        # Closing the splash is the commit point: browsers already show it gone, so the
        # uninstall runs to its end even if the caller is cancelled (S_01 section 3.2).
        return await _run_to_end(
            self._finish_reported_uninstall(service, token, package_id, versions)
        )

    async def _finish_reported_uninstall(
        self,
        service: PackageInstallReport,
        token: PackageOperationToken,
        package_id: str,
        versions: Sequence[str],
    ) -> ActivationReport:
        """Run the committed part of an uninstall and settle the report token."""

        report: ActivationReport | None = None
        failure: OperationError | None = None
        try:
            await self._close_splashes(package_id)
            self._runtime.stop_package(package_id)
            # Data goes first: if it cannot be removed the copies stay, so a repeated
            # uninstall can retry instead of reporting "not installed".
            try:
                await self._runtime.cleanup_package_data(package_id)
            except Exception as exc:  # noqa: BLE001 - keep the copies so a retry can remove them
                raise ExtensionFailure(
                    StatusCode.EXTENSION_DATA_WRITE_FAILED, reason=str(exc)
                ) from exc
            for version in versions:
                try:
                    with service.operation_scope(token):
                        await run_file_operation(
                            lambda version=version: self._remove_with_retry(
                                package_id, version, missing_ok=True
                            )
                        )
                except ExtensionFailure as exc:
                    failure = exc.as_error()
                    logger.warning(
                        "[GatewayExtensions] cannot delete %s/%s: %s",
                        package_id,
                        version,
                        exc,
                    )
            if failure is None and self._on_released is not None:
                try:
                    self._on_released(package_id)
                except Exception:  # noqa: BLE001 - view cleanup must not fail management
                    logger.exception("[GatewayExtensions] release cleanup failed for %s", package_id)
            report = (
                _success(package_id, "uninstall", active=None)
                if failure is None
                else _failure(package_id, "uninstall", failure)
            )
        except ExtensionFailure as exc:
            failure = exc.as_error()
            report = _failure(package_id, "uninstall", failure)
        except Exception as exc:  # noqa: BLE001 - a local failure is a report, not a crash
            logger.exception("[GatewayExtensions] cannot uninstall %s", package_id)
            report = _failure(
                package_id,
                "uninstall",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        finally:
            remaining = bool(self.gateway_installed(package_id))
            await _finish_operation(
                service,
                token,
                report=report,
                active=self._runtime.active_record(package_id),
                cleanup_pending=(report is None or report.status != "SUCCESS") and remaining,
            )
        return report or _failure(
            package_id,
            "uninstall",
            operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason="package was not removed"),
        )

    async def _switch(self, package: Any) -> None:
        """Replace the running version by ``package``, or leave it running (S_01 section 3.2).

        Preparing validates the new version and checks that its handlers can be registered;
        it does not await, so a failure there changes nothing. Closing the running splash is
        the commit point: it already changes what browsers show, so from there the switch
        runs to its end (commit, or abort when the commit fails or the runtime stopped) even
        if the caller is cancelled, and the cancellation is raised afterwards.
        """

        prepared = self._runtime.prepare(package)
        await _run_to_end(self._finish_switch(prepared))

    async def _finish_switch(self, prepared: Any) -> None:
        try:
            await self._close_splashes(prepared.package.package_id)
            self._runtime.commit(prepared)
        except BaseException:
            self._runtime.abort(prepared)
            raise

    async def _close_splashes(self, package_id: str) -> None:
        """Tell a package its playing splash ends before its handlers go away (S_10 section 5)."""

        if self._runtime.is_active_package(package_id):
            await self._runtime.browser.close_splashes(package_id, "removed")

    async def _publish_manifest(
        self, manifest: Manifest, root: Path, operation: str,
    ) -> ActivationReport:
        """Load and publish one store copy; failures keep the previous activation."""

        previous = self._runtime.active_record(manifest.package_id)
        try:
            await self._switch(load_gateway_package(root))
        except Exception as exc:  # noqa: BLE001 - one bad package must surface as a report
            logger.exception(
                "[GatewayExtensions] cannot %s %s", operation, manifest.package_id
            )
            return _failure(
                manifest.package_id,
                operation,
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        active = self._runtime.active_record(manifest.package_id)
        return _success(
            manifest.package_id,
            operation,
            active=active,
            previous_generation=None if previous is None else previous.generation,
        )

    async def _reject(self, manifest: Manifest, error: OperationError) -> None:
        assert self._report is not None
        try:
            await self._report.reject_package(manifest, error)
        except ExtensionFailure:
            logger.exception("[GatewayExtensions] cannot record rejection for %s", manifest.package_id)

    # ── legacy behaviour when no shared report is configured ──────────────

    async def _legacy_install_path(self, path: Path) -> ActivationReport:
        package_id = ""
        try:
            with self._inspection.open_source(path) as opened:
                package_id = opened.manifest.package_id
                if _domain_of(opened.manifest) != DOMAIN_GATEWAY:
                    return _failure(
                        package_id,
                        "install",
                        operation_error(
                            StatusCode.EXTENSION_DOMAIN_MISMATCH,
                            reason=f"package belongs to {_domain_of(opened.manifest)!r}",
                        ),
                    )
                root = self._store.copy_into_store(opened.root, opened.manifest)
                manifest = opened.manifest
        except ExtensionFailure as exc:
            return _failure(package_id or path.stem, "install", exc.as_error())
        except Exception as exc:  # noqa: BLE001 - report, never raise to the frame
            logger.exception("[GatewayExtensions] cannot install %s", path)
            return _failure(
                package_id or path.stem,
                "install",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        previous = self._runtime.active_record(package_id)
        if not manifest.enable:
            # A disabled copy is kept but never published.
            if self._runtime.is_active_package(package_id):
                await self._disable(root, manifest)
            return _success(package_id, "install", active=None)
        package = None
        try:
            package = load_gateway_package(root)
            await self._switch(package)
        except BaseException as exc:
            # The copy stays only if the switch committed (a cancellation after the commit
            # point still completes it); otherwise it must not be enabled at the next start.
            current = self._runtime.current_activation(package_id)
            if package is None or current is None or current.package is not package:
                try:
                    self._store.remove_installed(package_id, root.name)
                except ExtensionFailure:
                    logger.exception("[GatewayExtensions] cannot roll back %s", root)
            if not isinstance(exc, Exception):
                raise
            logger.exception("[GatewayExtensions] cannot publish %s", package_id)
            return _failure(
                package_id,
                "install",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        return _success(
            package_id,
            "install",
            active=self._runtime.active_record(package_id),
            previous_generation=None if previous is None else previous.generation,
        )

    async def _legacy_reload(self, package_id: str) -> ActivationReport:
        record = self._runtime.active_record(package_id)
        root = self._store.version_dir(package_id, record.version) if record is not None else None
        if root is None or not has_manifest(root):
            root = self._highest(package_id)
        if root is None:
            return _failure(
                package_id,
                "reload",
                operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
            )
        previous = record
        try:
            manifest = load_manifest(root, require_domain=True)
            if manifest.domain != DOMAIN_GATEWAY:
                raise ExtensionFailure(StatusCode.EXTENSION_DOMAIN_MISMATCH, package_id=package_id)
            if not manifest.enable:
                return _success(package_id, "reload", active=None)
            await self._switch(load_gateway_package(root))
        except Exception as exc:  # noqa: BLE001 - one bad package must surface as a report
            logger.exception("[GatewayExtensions] cannot reload %s", package_id)
            return _failure(
                package_id,
                "reload",
                operation_error(StatusCode.EXTENSION_LOAD_FAILED, reason=str(exc)),
            )
        return _success(
            package_id,
            "reload",
            active=self._runtime.active_record(package_id),
            previous_generation=None if previous is None else previous.generation,
        )

    async def _legacy_uninstall(self, package_id: str) -> ActivationReport:
        try:
            # A busy package keeps everything; nothing has changed yet.
            self._runtime.require_idle(package_id)
        except ExtensionFailure as exc:
            return _failure(package_id, "uninstall", exc.as_error())
        # Closing the splash is the commit point: from there the uninstall runs to its end
        # even if the caller is cancelled (S_01 section 3.2).
        return await _run_to_end(self._legacy_finish_uninstall(package_id))

    async def _legacy_finish_uninstall(self, package_id: str) -> ActivationReport:
        versions = self.gateway_installed(package_id)
        try:
            # The check repeats right before unpublishing because closing the splash
            # awaits (a call started meanwhile still keeps the package, though its
            # splash has closed).
            await self._close_splashes(package_id)
            released = self._runtime.stop_package(package_id)
        except ExtensionFailure as exc:
            return _failure(package_id, "uninstall", exc.as_error())
        if not versions and not released:
            return _failure(
                package_id,
                "uninstall",
                operation_error(StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED, package_id=package_id),
            )
        try:
            # Data goes first: if it cannot be removed the copies stay, so
            # a repeated uninstall can retry instead of reporting "not installed".
            await self._runtime.cleanup_package_data(package_id)
        except Exception as exc:  # noqa: BLE001 - surface as a report and keep the copies
            logger.warning("[GatewayExtensions] cannot delete data of %s: %s", package_id, exc)
            return _failure(
                package_id,
                "uninstall",
                operation_error(StatusCode.EXTENSION_DATA_WRITE_FAILED, reason=str(exc)),
            )
        failure: OperationError | None = None
        for package in versions:
            try:
                await asyncio.to_thread(self._remove_with_retry, package.package_id, package.version)
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
        return _success(package_id, "uninstall", active=None)

    # ── internals ─────────────────────────────────────────────────────────

    def _highest(self, package_id: str) -> Path | None:
        versions = self.gateway_installed(package_id)
        if not versions:
            return None
        return max(versions, key=lambda package: _version_key(package.version)).root

    async def _enable(self, root: Path, manifest: Manifest) -> ActivationReport:
        """Persist true, then publish the selected version; undo on failure."""
        package_id = manifest.package_id
        active = self._runtime.active_record(package_id)
        self._store.set_enabled(package_id, manifest.version, True)
        if active is not None:
            return _success(package_id, "enable", active=active)
        package = None
        try:
            package = load_gateway_package(root)
            await self._switch(package)
        except BaseException as exc:
            current = self._runtime.current_activation(package_id)
            if package is None or current is None or current.package is not package:
                self._store.set_enabled(package_id, manifest.version, False)
            if not isinstance(exc, Exception):
                raise
            raise ExtensionFailure(
                StatusCode.EXTENSION_LOAD_FAILED,
                package_id=package_id,
                reason=str(exc),
                cause=exc,
            ) from exc
        return _success(package_id, "enable", active=self._runtime.active_record(package_id))

    async def _disable(self, root: Path, manifest: Manifest) -> ActivationReport:
        """Unpublish the selected package without deleting its install copy."""
        del root
        package_id = manifest.package_id
        released = self._runtime.unpublish(package_id)
        self._store.set_enabled(package_id, manifest.version, False)
        if released and self._on_released is not None:
            try:
                self._on_released(package_id)
            except Exception:  # noqa: BLE001 - view cleanup must not fail management
                logger.exception("[GatewayExtensions] release cleanup failed for %s", package_id)
        return _success(package_id, "disable", active=None)

    def _find_root(self, package_id: str, version: str) -> Path | None:
        for package in self.gateway_installed(package_id):
            if package.version == version:
                return package.root
        return None

    def _manifest_for(
        self,
        package_id: str,
        record: ActivePackageRecord | None,
        versions: Sequence[str],
    ) -> Manifest | None:
        if record is not None:
            return Manifest(
                schema_version=1,
                package_id=record.package_id,
                version=record.version,
                display_name=None,
                description=None,
                requires_host=None,
                requires_packages=record.requires_packages,
                extensions=(),
                domain=record.domain,
            )
        roots = [
            item.root for item in self.gateway_installed(package_id) if item.version in versions
        ]
        for root in reversed(roots):
            try:
                return load_manifest(root, require_domain=False)
            except ExtensionFailure:
                continue
        if versions:
            return Manifest(
                schema_version=1,
                package_id=package_id,
                version=sorted(versions, key=_version_key)[-1],
                display_name=None,
                description=None,
                requires_host=None,
                requires_packages=(),
                extensions=(),
                domain=DOMAIN_GATEWAY,
            )
        return None

    def _remove_with_retry(
        self, package_id: str, version: str, *, missing_ok: bool = False,
    ) -> None:
        """Delete one copy, retrying transient file locks held by other processes."""

        for attempt in range(1, _DELETE_ATTEMPTS + 1):
            try:
                if missing_ok:
                    self._store.remove_installed(package_id, version, missing_ok=True)
                else:
                    self._store.remove_installed(package_id, version)
                return
            except ExtensionFailure:
                if attempt == _DELETE_ATTEMPTS:
                    raise
                time.sleep(_DELETE_BACKOFF_SECONDS * attempt)


async def _run_to_end(operation: Awaitable[Any]) -> Any:
    """Await ``operation`` past every cancellation of the caller, then re-raise the first one.

    Used after a management operation's commit point, where stopping halfway would leave
    browsers and packages in a state no version describes. Like agent-core's
    ``run_file_operation``, it keeps waiting until the task has really finished (a repeated
    cancellation must not detach it) and reads its result so a failure is logged, not lost.
    """

    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - read below; the cancellation is what propagates
                break
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "[GatewayExtensions] management operation failed after its caller was cancelled",
                exc_info=task.exception(),
            )
        raise cancelled


async def _finish_operation(
    service: PackageInstallReport,
    token: PackageOperationToken,
    *,
    report: ActivationReport | None,
    active: ActivePackageRecord | None,
    cleanup_pending: bool,
) -> None:
    """Commit a completed package result without hiding that local result."""

    try:
        await service.finish_operation(
            token,
            report=report,
            active=active,
            cleanup_pending=cleanup_pending,
        )
    except ExtensionFailure:
        logger.exception("could not commit package operation result")


def _success(
    package_id: str,
    operation: str,
    *,
    active: ActivePackageRecord | None,
    previous_generation: int | None = None,
) -> ActivationReport:
    return ActivationReport(
        plan_id="",
        package_id=package_id,
        operation=operation,
        status="SUCCESS",
        previous_generation=previous_generation,
        current_generation=None if active is None else active.generation,
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


def _domain_of(manifest: Manifest) -> str:
    value = (manifest.domain or "").strip().lower()
    return value or DOMAIN_AGENT_SERVER


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def _record_recovery_change(
    install_dir: Path,
    data_dir: Path | None,
    action: str,
    reports: Sequence[ActivationReport],
) -> None:
    """Persist successful transitions without affecting Gateway package results."""
    package_ids = [report.package_id for report in reports if report.status == "SUCCESS"]
    if not package_ids:
        return
    try:
        SystemPluginRecovery(install_dir, data_dir).record(action, package_ids)
    except Exception:  # noqa: BLE001 - recovery history must not break a successful operation
        logger.warning("[GatewayExtensions] could not record system plugin recovery history", exc_info=True)


def _host_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("jiuwenswarm")
    except Exception:  # noqa: BLE001 - a source checkout may not be installed
        return None
