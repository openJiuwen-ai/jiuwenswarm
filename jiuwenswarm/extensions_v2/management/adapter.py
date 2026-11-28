# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer requests for the already assembled V2 package manager."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.bundle import bundle_version_dir, is_bundle_document
from openjiuwen.core.extension.data.json_values import valid_package_id
from openjiuwen.core.extension.errors import OperationError
from openjiuwen.core.extension.management import QueryResult
from openjiuwen.core.extension.package_manager import PackageManager

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.assembly import get_extension_manager
from jiuwenswarm.extensions_v2.config import resolve_examples_dir, resolve_install_dir
from jiuwenswarm.extensions_v2.management import containers
from jiuwenswarm.extensions_v2.management.catalog import (
    PackageNotFound,
    StateIndex,
    build_state_index,
    entry_from_root,
    find_entry,
    installed_version,
    list_catalog,
    list_mine,
    merge_versions,
    project_detail,
    resolve_install_source,
)
from jiuwenswarm.extensions_v2.management.hub_catalog import (
    get_hub_system_plugin,
    hub_detail,
    hub_file_content,
    hub_file_tree,
    list_hub_system_plugins,
)
from jiuwenswarm.extensions_v2.management.recovery import query_after_error
from jiuwenswarm.extensions_v2.management.registry import CODE_MANAGER_NOT_INITIALIZED, ExtensionManager
from jiuwenswarm.extensions_v2.manifest_io import (
    DOMAIN_AGENT_SERVER,
    DOMAIN_GATEWAY,
    package_domain,
    read_manifest_document,
)
from jiuwenswarm.server.runtime.extension_package_manager import (
    list_package_files,
    read_package_file,
)
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ActiveInstance,
    ExtensionBundleRecord,
    ExtensionInstallRecord,
    ExtensionInstallState,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import HubNotFoundError
from jiuwenswarm.server.runtime.marketplace.hub_extensions import download_system_plugin_archive
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)

_PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"


def _require_all_members(request: AgentRequest) -> bool:
    """True when the forwarding Gateway loads its own container members."""

    metadata = request.metadata
    return isinstance(metadata, dict) and metadata.get("extensions_v2_require_all_members") is True


@dataclass(frozen=True, slots=True)
class _Request:
    paths: tuple[Path, ...] = ()
    package_ids: tuple[str, ...] = ()
    package_id: str = ""
    path: str = ""
    scope: str = "catalog"
    refresh: bool = False
    enable: bool | None = None
    version: str = ""
    inspect: bool = False


class PackageManagementAdapter(GatewayAdapter):
    """Validate management requests without reimplementing the Core lifecycle."""

    methods: ClassVar[frozenset[str]] = frozenset(
        {
            ReqMethod.EXTENSIONS_V2_QUERY.value,
            ReqMethod.EXTENSIONS_V2_CATALOG.value,
            ReqMethod.EXTENSIONS_V2_SHOW.value,
            ReqMethod.EXTENSIONS_V2_IMPORT.value,
            ReqMethod.EXTENSIONS_V2_INSTALL.value,
            ReqMethod.EXTENSIONS_V2_UNINSTALL.value,
            ReqMethod.EXTENSIONS_V2_RELOAD.value,
            ReqMethod.EXTENSIONS_V2_SET_ENABLED.value,
            ReqMethod.EXTENSIONS_V2_FILE_LIST.value,
            ReqMethod.EXTENSIONS_V2_FILE_READ.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method.value if isinstance(request.req_method, ReqMethod) else request.req_method
        try:
            parsed = _parse_request(method, request.params)
        except ValueError as exc:
            return _error_response(request, str(exc), code="INVALID_PARAMS")

        manager = get_extension_manager()
        if manager is None or not manager.initialized:
            return _error_response(request, "V2 package manager is unavailable", code=CODE_MANAGER_NOT_INITIALIZED)

        try:
            require_all_members = _require_all_members(request)
            payload = await _dispatch(manager, method, parsed, require_all_members=require_all_members)
        except PackageNotFound as exc:
            return _error_response(request, f"package {exc} not found", code=_PACKAGE_NOT_FOUND)
        except ValueError as exc:
            return _error_response(request, str(exc), code="INVALID_PARAMS")
        except ExtensionFailure as exc:
            return await _failed_request(request, manager, parsed.package_id, exc.message, code=exc.status.name)
        except Exception as exc:  # noqa: BLE001 - keep management failures at the request boundary
            if method == ReqMethod.EXTENSIONS_V2_SHOW.value and isinstance(exc, HubNotFoundError):
                return _error_response(request, str(exc), code=_PACKAGE_NOT_FOUND)
            logger.exception("[ExtensionsV2] management request failed: %s", method)
            return await _failed_request(request, manager, parsed.package_id, "V2 package management failed")
        should_notify = method in (ReqMethod.EXTENSIONS_V2_INSTALL.value, ReqMethod.EXTENSIONS_V2_IMPORT.value) or (
            method == ReqMethod.EXTENSIONS_V2_SET_ENABLED.value and parsed.enable is True
        )
        if should_notify and not parsed.inspect:
            # The package result is already committed; a notification-side failure
            # (config or state-store read) must not turn it into an error response.
            try:
                store = ExtensionInstallState.for_install_dir(resolve_install_dir())
                payload["ui_notified"] = await _notify_installed_packages(
                    payload.get("reports", ()), store,
                )
            except Exception:  # noqa: BLE001 - keep the management response authoritative
                logger.warning("[ExtensionsV2] package change UI notification failed", exc_info=True)
                payload["ui_notified"] = False
        try:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=_encode(payload),
                metadata=request.metadata,
                agent_ref=request.agent_ref,
            )
        except Exception:  # noqa: BLE001 - a received Report is not an unknown operation result
            logger.exception("[ExtensionsV2] could not encode management result")
            return _error_response(request, "Could not encode V2 package management result")


async def _dispatch(
    manager: ExtensionManager,
    method: str,
    parsed: _Request,
    *,
    require_all_members: bool = False,
) -> dict[str, Any]:
    if method == ReqMethod.EXTENSIONS_V2_CATALOG.value:
        state = await manager.query()
        examples_dir = resolve_examples_dir()
        install_dir = resolve_install_dir()
        packages = (
            list_catalog(examples_dir, state, install_dir=install_dir)
            if parsed.scope == "catalog"
            else list_mine(examples_dir, state, install_dir=install_dir)
        )
        cache = None
        if parsed.scope == "catalog":
            local_ids = {str(item.get("id")) for item in packages}
            hub_packages, cache = await list_hub_system_plugins(
                state, install_dir, refresh=parsed.refresh
            )
            packages.extend(
                item
                for item in hub_packages
                if item["id"] not in local_ids and item.get("packageName") not in local_ids
            )
        return {"packages": packages, **({"cache": cache} if cache else {})}
    if method == ReqMethod.EXTENSIONS_V2_SHOW.value:
        state = await manager.query()
        install_dir = resolve_install_dir()
        store = ExtensionInstallState.for_install_dir(install_dir)
        bundle_record = (
            store.get_bundle(parsed.package_id, parsed.version)
            if parsed.version
            else store.active_bundle(parsed.package_id)
        ) or store.get_bundle(parsed.package_id)
        try:
            entry = find_entry(
                parsed.package_id,
                resolve_examples_dir(),
                state,
                install_dir=install_dir,
                accepted_roots=manager.accepted_roots(parsed.package_id),
                version=parsed.version,
            )
        except PackageNotFound:
            if bundle_record is not None:
                detail = await get_hub_system_plugin(
                    bundle_record.asset_id or bundle_record.bundle_id, parsed.version,
                )
                return {"package": hub_detail(detail, state, store)}
            record = store.get(parsed.package_id) or store.by_asset(parsed.package_id)
            hub_key = record.asset_id if record is not None and record.asset_id else parsed.package_id
            detail = await get_hub_system_plugin(hub_key, parsed.version)
            return {"package": hub_detail(detail, state, store)}
        detail = project_detail(entry, state, install_dir=install_dir)
        if entry.is_bundle:
            if bundle_record is not None and bundle_record.asset_id:
                try:
                    hub = await get_hub_system_plugin(bundle_record.asset_id, "")
                    hub_projected = hub_detail(hub, state, store)
                    detail["versions"] = merge_versions(
                        detail.get("versions") or [], hub_projected.get("versions") or [],
                    )
                    detail["preview"] = detail.get("preview") or hub_projected.get("preview")
                except Exception:  # noqa: BLE001 - installed details must survive Hub outages
                    logger.warning(
                        "[ExtensionsV2] Hub detail unavailable for %s",
                        bundle_record.asset_id,
                        exc_info=True,
                    )
            return {"package": detail}
        record = store.get(entry.package_id, detail.get("installedVersion", "")) or store.get(entry.package_id)
        if record is None or record.origin != "hub" or not record.asset_id:
            return {"package": detail}
        if parsed.version and parsed.version != detail.get("installedVersion"):
            hub = await get_hub_system_plugin(record.asset_id, parsed.version)
            return {"package": hub_detail(hub, state, store)}
        try:
            hub = await get_hub_system_plugin(record.asset_id, "")
            hub_projected = hub_detail(hub, state, store)
            detail["versions"] = merge_versions(
                detail.get("versions") or [], hub_projected.get("versions") or [],
            )
            detail["preview"] = detail.get("preview") or hub_projected.get("preview")
        except Exception:  # noqa: BLE001 - installed details must survive Hub outages
            logger.warning("[ExtensionsV2] Hub detail unavailable for %s", record.asset_id, exc_info=True)
        return {"package": detail}
    if method in (ReqMethod.EXTENSIONS_V2_FILE_LIST.value, ReqMethod.EXTENSIONS_V2_FILE_READ.value):
        state = await manager.query()
        install_dir = resolve_install_dir()
        store = ExtensionInstallState.for_install_dir(install_dir)
        bundle_record = _bundle_by_asset(store, parsed.package_id)
        record = store.get(parsed.package_id) or (
            None if bundle_record is not None else store.by_asset(parsed.package_id)
        )
        package_id = (
            bundle_record.bundle_id
            if bundle_record is not None
            else record.package_id if record is not None else parsed.package_id
        )
        installed = installed_version(build_state_index(state, store), package_id) or (
            record.version if record is not None else ""
        )
        record = store.get(package_id, installed) or record
        if record is not None and record.origin == "hub":
            if parsed.version and parsed.version != installed:
                return await _hub_file_payload(
                    record.asset_id or package_id, method, parsed.path, parsed.version
                )
        try:
            entry = find_entry(
                package_id,
                resolve_examples_dir(),
                state,
                install_dir=install_dir,
                accepted_roots=manager.accepted_roots(package_id),
                version=parsed.version,
            )
        except PackageNotFound:
            return await _hub_file_payload(
                record.asset_id if record is not None and record.asset_id else package_id,
                method,
                parsed.path,
                parsed.version,
            )
        if method == ReqMethod.EXTENSIONS_V2_FILE_LIST.value:
            return {"tree": list_package_files(entry.root)}
        return read_package_file(entry.root, parsed.path)
    if method == ReqMethod.EXTENSIONS_V2_IMPORT.value and parsed.inspect:
        package_id = await asyncio.to_thread(_inspect_import_package, parsed.paths[0])
        return {"package_id": package_id}
    if method in (ReqMethod.EXTENSIONS_V2_INSTALL.value, ReqMethod.EXTENSIONS_V2_IMPORT.value):
        install_dir = resolve_install_dir()
        store = ExtensionInstallState.for_install_dir(install_dir)
        outcomes: list[containers.ContainerOutcome] = []
        if parsed.paths:
            outcomes.extend(
                await containers.install_sources(
                    manager,
                    store,
                    parsed.paths,
                    origin="user",
                    version=parsed.version,
                    timeout=_dependency_timeout(),
                    preempt=False,
                    require_all_members=require_all_members,
                    reject_installed=True,
                )
            )
        if parsed.package_ids:
            outcomes.extend(
                await _install_package_containers(
                    manager, store, parsed, install_dir, require_all_members=require_all_members,
                )
            )
        reports = [report for outcome in outcomes for report in outcome.reports]
        payload: dict[str, Any] = {
            "reports": reports,
            "installed_packages": _installed_packages(manager, reports),
        }
        if len(outcomes) == 1:
            payload["bundle"] = outcomes[0].projection()
        elif outcomes:
            payload["bundles"] = [outcome.projection() for outcome in outcomes]
        return payload
    if method == ReqMethod.EXTENSIONS_V2_UNINSTALL.value:
        store = ExtensionInstallState.for_install_dir(resolve_install_dir())
        if parsed.version:
            bundle = store.get_bundle(parsed.package_id, parsed.version)
            if bundle is not None:
                return await containers.uninstall_container(manager, store, bundle)
            if store.get_bundle(parsed.package_id) is not None:
                raise PackageNotFound(parsed.package_id)
        else:
            bundles = [
                record for record in store.bundles()
                if record.bundle_id == parsed.package_id
            ]
            if bundles:
                reports: list[Any] = []
                for record in sorted(bundles, key=lambda item: item.active):
                    result = await containers.uninstall_container(manager, store, record)
                    reports.extend(result.get("reports") or ())
                    if result.get("bundle") is not None:
                        return {"reports": reports, "bundle": result["bundle"]}
                return {"reports": reports, "bundle": None}
        record = store.get(parsed.package_id, parsed.version)
        if record is not None and record.domain == DOMAIN_GATEWAY:
            # The Gateway owns its runtime; mark the copy for the Gateway to
            # unload and delete through its own in-flight-safe uninstall path.
            store.mark(
                parsed.package_id, version=parsed.version, active=False, enabled=False,
                status="uninstalling",
            )
            if record.version:
                await containers.promote_same_version_standby(
                    manager, store, parsed.package_id, record.version,
                )
            return {"reports": [_success_report(parsed.package_id, "uninstall")]}
        report = await (
            manager.uninstall(parsed.package_id, parsed.version)
            if parsed.version
            else manager.uninstall(parsed.package_id)
        )
        if report.status == "SUCCESS":
            store.remove(parsed.package_id, parsed.version)
            if record is not None and record.version:
                await containers.promote_same_version_standby(
                    manager, store, parsed.package_id, record.version,
                )
        return {"reports": [report]}
    if method == ReqMethod.EXTENSIONS_V2_RELOAD.value:
        store = ExtensionInstallState.for_install_dir(resolve_install_dir())
        bundle = _selected_bundle(store, parsed)
        if bundle is not None:
            return await containers.apply_container_action(manager, store, bundle, "reload")
        if parsed.version and store.get_bundle(parsed.package_id) is not None:
            raise PackageNotFound(parsed.package_id)
        if parsed.version:
            raise ValueError("version is only supported for bundle packages")
        record = store.get(parsed.package_id)
        if record is not None and record.domain == DOMAIN_GATEWAY:
            store.mark(parsed.package_id, status="pending")
            return {"reports": [_success_report(parsed.package_id, "reload")]}
        report = await manager.reload(parsed.package_id)
        if report.status == "SUCCESS":
            version = _active_version(manager, parsed.package_id)
            if version:
                store.mark(parsed.package_id, version=version, active=True, enabled=True)
        return {"reports": [report]}
    if method == ReqMethod.EXTENSIONS_V2_SET_ENABLED.value:
        store = ExtensionInstallState.for_install_dir(resolve_install_dir())
        bundle = _selected_bundle(store, parsed)
        if bundle is not None:
            return await containers.apply_container_action(
                manager, store, bundle, "set_enabled", enable=parsed.enable is True,
                require_all_members=require_all_members,
            )
        if parsed.version and store.get_bundle(parsed.package_id) is not None:
            raise PackageNotFound(parsed.package_id)
        if parsed.version:
            raise ValueError("version is only supported for bundle packages")
        record = store.get(parsed.package_id)
        if record is not None and record.domain == DOMAIN_GATEWAY:
            if parsed.enable is True:
                store.switch_active_instance(
                    ActiveInstance(
                        package_id=parsed.package_id,
                        version=record.version,
                        kind="standalone",
                    )
                )
            else:
                store.disable_instance(
                    parsed.package_id, kind="standalone", version=record.version,
                )
            store.mark(
                parsed.package_id,
                enabled=parsed.enable is True,
                status="pending" if parsed.enable is True else "disabled",
            )
            if parsed.enable is not True:
                await containers.promote_same_version_standby(
                    manager, store, parsed.package_id, record.version,
                )
            return {"reports": [_success_report(parsed.package_id, "enable" if parsed.enable else "disable")]}
        active_version = _active_version(manager, parsed.package_id)
        claim = store.active_instance(parsed.package_id)
        if parsed.enable is True and claim is not None and claim.kind != "standalone":
            # A bundle member currently owns the id; an explicit enable of the
            # standalone takes it over instead of leaving the runtime and the
            # claim disagreeing about which copy runs.
            try:
                await manager.disable_version(parsed.package_id, claim.version)
            except Exception:  # noqa: BLE001 - the target load decides the result
                logger.warning(
                    "[ExtensionsV2] %s could not be released for the standalone",
                    parsed.package_id,
                    exc_info=True,
                )
        report = await manager.set_enabled(parsed.package_id, parsed.enable is True)
        if report.status == "SUCCESS":
            if record is not None and record.install_path and record.domain != DOMAIN_GATEWAY:
                await asyncio.to_thread(
                    containers.set_manifest_enable,
                    Path(record.install_path),
                    parsed.enable is True,
                )
            if parsed.enable is True:
                version = _active_version(manager, parsed.package_id)
                if version:
                    store.mark(
                        parsed.package_id, version=version, active=True, enabled=True,
                        status="active",
                    )
                    store.switch_active_instance(
                        ActiveInstance(
                            package_id=parsed.package_id,
                            version=version,
                            kind="standalone",
                        )
                    )
            else:
                store.mark(parsed.package_id, active=False, enabled=False, status="disabled")
                if active_version:
                    store.disable_instance(
                        parsed.package_id, kind="standalone", version=active_version,
                    )
                await containers.promote_same_version_standby(
                    manager, store, parsed.package_id, active_version or record.version,
                )
        return {"reports": [report]}
    return {"state": _agentserver_query_result(await manager.query(parsed.package_id or None))}


async def _install_package_containers(
    manager: ExtensionManager,
    store: ExtensionInstallState,
    parsed: _Request,
    install_dir: Path,
    *,
    require_all_members: bool = False,
) -> list[containers.ContainerOutcome]:
    """Resolve ``package_ids`` to sources, then install them as containers."""

    examples_dir = resolve_examples_dir()
    outcomes: list[containers.ContainerOutcome] = []
    temps: list[tempfile.TemporaryDirectory[str]] = []
    try:
        resolved = [
            await _resolve_container_source(
                requested_id, parsed.version, install_dir, examples_dir, store, temps,
            )
            for requested_id in parsed.package_ids
        ]
        containers.reject_batch_cycles(install_dir, store, [source for source, _, _ in resolved])
        for source, origin, asset_id in resolved:
            outcomes.extend(
                await containers.install_sources(
                    manager,
                    store,
                    [source],
                    origin=origin,
                    asset_id=asset_id,
                    version=parsed.version,
                    timeout=_dependency_timeout(),
                    require_all_members=require_all_members,
                )
            )
    finally:
        for temporary in temps:
            temporary.cleanup()
    return outcomes


async def _resolve_container_source(
    requested_id: str,
    version: str,
    install_dir: Path,
    examples_dir: Path,
    store: ExtensionInstallState,
    temps: list[tempfile.TemporaryDirectory[str]],
) -> tuple[Path, str, str]:
    """Resolve one package id to a local directory, preset or Hub archive."""

    current = store.get(requested_id) or store.by_asset(requested_id)
    package_id = current.package_id if current is not None else requested_id
    bundle = (
        store.get_bundle(requested_id, version)
        if version
        else store.active_bundle(requested_id)
    )
    if bundle is not None:
        root = bundle_version_dir(install_dir, bundle.bundle_id, bundle.version)
        if root.is_dir():
            return root, bundle.origin, bundle.asset_id
    if version:
        stored = install_dir / package_id / version
        if stored.is_dir():
            origin = current.origin if current is not None else "preset"
            asset_id = current.asset_id if current is not None else ""
            return stored, origin, asset_id
    try:
        source = resolve_install_source(requested_id, examples_dir=examples_dir, install_dir=install_dir)
        entry = entry_from_root(source)
        if version and (entry is None or entry.version != version):
            raise PackageNotFound(requested_id)
        in_examples = source.resolve().is_relative_to(examples_dir.resolve())
        origin = current.origin if current is not None else ("preset" if in_examples else "user")
        asset_id = current.asset_id if current is not None else ""
        return source, origin, asset_id
    except PackageNotFound:
        pass
    key = current.asset_id if current is not None and current.asset_id else requested_id
    detail = await get_hub_system_plugin(key, version)
    package_id = detail.package_name or requested_id
    target_version = detail.version
    stored = install_dir / package_id / target_version
    if stored.is_dir():
        return stored, "hub", detail.asset_id
    temporary = tempfile.TemporaryDirectory(prefix="ojw_system_plugin_")
    temps.append(temporary)
    archive = Path(temporary.name) / f"{package_id}-{target_version}.zip"
    await download_system_plugin_archive(detail, archive)
    return archive, "hub", detail.asset_id



def _dependency_timeout() -> float:
    from jiuwenswarm.extensions_v2.config import resolve_install_report_settings

    return float(resolve_install_report_settings().dependency_timeout)


def _success_report(package_id: str, operation: str) -> ActivationReport:
    """A host-side acknowledgement for a package the other host will load."""

    return ActivationReport("", package_id, operation, "SUCCESS", None, None)


def _selected_bundle(
    store: ExtensionInstallState, parsed: _Request,
) -> ExtensionBundleRecord | None:
    """Return the bundle version a management request targets.

    The detail page acts on the version the user is looking at; a request
    without ``version`` keeps the previous active-or-newest behaviour used by
    the market cards.
    """

    if parsed.version:
        return store.get_bundle(parsed.package_id, parsed.version)
    return store.active_bundle(parsed.package_id) or store.get_bundle(parsed.package_id)


def _active_version(manager: ExtensionManager, package_id: str) -> str:
    for record in manager.active_packages():
        if record.package_id == package_id:
            return str(record.version)
    return ""


async def _hub_file_payload(
    package_id: str, method: str, path: str, version: str = ""
) -> dict[str, Any]:
    """Serve file previews for Hub-only packages without downloading the archive."""
    try:
        if method == ReqMethod.EXTENSIONS_V2_FILE_LIST.value:
            return {"tree": await hub_file_tree(package_id, version=version)}
        return await hub_file_content(package_id, path, version=version)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - a Hub outage must not break the detail page
        logger.warning("[ExtensionsV2] Hub file preview failed: %s", package_id, exc_info=True)
        if method == ReqMethod.EXTENSIONS_V2_FILE_LIST.value:
            return {"tree": []}
        raise ValueError("file preview is unavailable") from exc


async def _notify_installed_packages(
    reports: Sequence[ActivationReport],
    store: ExtensionInstallState | None = None,
) -> bool:
    """Refresh mounted WebUI slots after a package change.

    A failed version switch restores the previously active version, so that
    package must replay ``ui.ready`` even though its Report says FAILED.
    """
    package_ids = {report.package_id for report in reports if report.status == "SUCCESS"}
    for report in reports:
        if report.status == "SUCCESS" or store is None:
            continue
        # Judge by the live instance, not by a versionless record lookup: a
        # failed switch leaves several versions behind and only the restored
        # one still has an owner.
        claim = store.active_instance(report.package_id)
        if claim is not None or store.active_bundle(report.package_id) is not None:
            package_ids.add(report.package_id)
    if not package_ids:
        return False
    from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

    try:
        runtime = get_webui_runtime()
        return await runtime.notify_packages_changed(sorted(package_ids)) if runtime is not None else False
    except Exception:  # noqa: BLE001 - notification failure does not undo a successful install
        logger.warning("[ExtensionsV2] package change UI notification failed", exc_info=True)
        return False


def _installed_packages(
    manager: ExtensionManager,
    reports: Sequence[ActivationReport],
) -> list[dict[str, str | int]]:
    """Return actual roots for the generations this install activated."""

    succeeded = {
        (report.package_id, report.current_generation)
        for report in reports
        if report.status == "SUCCESS"
    }
    if not succeeded:
        return []
    return [
        {
            "package_id": record.package_id,
            "version": record.version,
            "install_path": str(record.root),
            "generation": record.generation,
        }
        for record in manager.active_packages()
        if (record.package_id, record.generation) in succeeded
    ]


def _inspect_import_package(path: Path) -> str | None:
    try:
        with PackageManager(path.parent).open_source(path) as opened:
            return opened.manifest.package_id
    except ExtensionFailure:
        # The regular import still owns validation and error reporting.
        return None


def _parse_request(method: object, params: object) -> _Request:
    if not isinstance(method, str) or method not in PackageManagementAdapter.methods:
        raise ValueError("unsupported V2 package management method")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    if method == ReqMethod.EXTENSIONS_V2_IMPORT.value:
        inspect = params.get("inspect", False)
        if set(params) - {"path", "inspect"} or "path" not in params or not isinstance(inspect, bool):
            raise ValueError("import requires one absolute ZIP path")
        return _Request(paths=tuple(_zip_paths([params["path"]])), inspect=inspect)

    if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
        if set(params) == {"paths"}:
            return _Request(paths=tuple(_zip_paths(params["paths"])))
        if set(params) in ({"package_ids"}, {"package_ids", "version"}):
            return _Request(
                package_ids=tuple(_package_ids(params["package_ids"])),
                version=_version(params.get("version", "")),
            )
        raise ValueError("install requires paths or package_ids with an optional version")

    if method == ReqMethod.EXTENSIONS_V2_CATALOG.value:
        if set(params) - {"scope", "refresh"}:
            raise ValueError("catalog accepts scope and refresh only")
        scope = params.get("scope", "catalog")
        if scope not in {"catalog", "mine"}:
            raise ValueError("scope must be 'catalog' or 'mine'")
        refresh = params.get("refresh", False)
        if not isinstance(refresh, bool):
            raise ValueError("refresh must be a boolean")
        return _Request(scope=scope, refresh=refresh)

    if method == ReqMethod.EXTENSIONS_V2_SHOW.value:
        if set(params) - {"package_id", "version"} or "package_id" not in params:
            raise ValueError("show accepts package_id and an optional version")
        return _Request(
            package_id=_package_id(params["package_id"]),
            version=_version(params.get("version")),
        )

    if method == ReqMethod.EXTENSIONS_V2_FILE_LIST.value:
        if set(params) - {"package_id", "version"} or "package_id" not in params:
            raise ValueError("file list accepts package_id and an optional version")
        return _Request(
            package_id=_package_id(params["package_id"]),
            version=_version(params.get("version")),
        )

    if method == ReqMethod.EXTENSIONS_V2_FILE_READ.value:
        if set(params) - {"package_id", "path", "version"} or "package_id" not in params:
            raise ValueError("file read accepts package_id, path and an optional version")
        relative = params.get("path")
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("path must be a nonempty string")
        return _Request(
            package_id=_package_id(params["package_id"]),
            path=relative.strip(),
            version=_version(params.get("version")),
        )

    if method == ReqMethod.EXTENSIONS_V2_SET_ENABLED.value:
        if set(params) - {"package_id", "enable", "version"} or "package_id" not in params:
            raise ValueError("set enabled requires package_id, enable and an optional version")
        enable = params.get("enable")
        if not isinstance(enable, bool):
            raise ValueError("enable must be a boolean")
        return _Request(
            package_id=_package_id(params["package_id"]),
            enable=enable,
            version=_version(params.get("version")),
        )

    if method == ReqMethod.EXTENSIONS_V2_RELOAD.value:
        if set(params) - {"package_id", "version"} or "package_id" not in params:
            raise ValueError("reload requires package_id and an optional version")
        return _Request(
            package_id=_package_id(params["package_id"]),
            version=_version(params.get("version")),
        )

    if method == ReqMethod.EXTENSIONS_V2_UNINSTALL.value:
        if set(params) - {"package_id", "version"} or "package_id" not in params:
            raise ValueError("uninstall requires package_id and an optional version")
        return _Request(
            package_id=_package_id(params["package_id"]),
            version=_version(params.get("version")),
        )

    if set(params) - {"package_id"}:
        raise ValueError("only package_id is accepted")
    if method == ReqMethod.EXTENSIONS_V2_QUERY.value and "package_id" not in params:
        return _Request()
    return _Request(package_id=_package_id(params.get("package_id")))


def _version(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("version must be a string")
    version = value.strip()
    if not version:
        return ""
    invalid_separator = "/" in version or "\\" in version
    if len(version) > 64 or any(ch.isspace() for ch in version) or invalid_separator:
        raise ValueError("version must be 1-64 characters without spaces or slashes")
    return version


def _package_id(value: object) -> str:
    if not isinstance(value, str) or not valid_package_id(value):
        raise ValueError(
            "package_id must use 1-128 letters, digits, dots, underscores or hyphens, without spaces or '..'"
        )
    return value


def _package_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("package_ids must be a nonempty array of string ids")
    return [_package_id(item) for item in value]


def _zip_paths(value: object) -> list[Path]:
    if not isinstance(value, list) or not value:
        raise ValueError("paths must be a nonempty array of absolute paths")
    paths: list[Path] = []
    for raw_path in value:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("each path must be a nonempty string")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("install requires an absolute ZIP file path")
        if path.suffix.lower() != ".zip" or not path.is_file():
            raise ValueError("install accepts existing .zip files only; directories are not supported")
        paths.append(path)
    return paths


def _error_response(
    request: AgentRequest,
    error: str,
    *,
    code: str = "INTERNAL_ERROR",
    extra: dict[str, Any] | None = None,
) -> AgentResponse:
    response = build_error_response(request, error, code=code, extra=extra)
    response.agent_ref = request.agent_ref
    return response


async def _failed_request(
    request: AgentRequest,
    manager: ExtensionManager,
    package_id: str,
    error: str,
    *,
    code: str = "INTERNAL_ERROR",
) -> AgentResponse:
    if request.req_method in (ReqMethod.EXTENSIONS_V2_QUERY, ReqMethod.EXTENSIONS_V2_QUERY.value):
        return _error_response(request, error, code=code)
    extra: dict[str, Any] = {"outcome": "unknown"}
    state, query_error = await query_after_error(manager, package_id or None)
    if query_error is not None:
        extra["query_error"] = query_error
    else:
        try:
            extra["state"] = _encode(_agentserver_query_result(state))
        except Exception:  # noqa: BLE001 - keep the original operation error if state encoding fails
            logger.exception("[ExtensionsV2] could not encode current package state")
            extra["query_error"] = "Current package state could not be confirmed"
    return _error_response(request, error, code=code, extra=extra)


def _encode(value: object) -> Any:
    """Encode Core value objects without publishing their implementation enums."""

    if isinstance(value, QueryResult):
        state = {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
        state["package_ids"] = sorted(record.package_id for record in value.packages if record.successful)
        state["installed_packages"] = [
            {
                "package_id": root.parent.name,
                "version": root.name,
                "domain": _installed_package_domain(root),
                "install_path": str(root),
            }
            for root in value.installed
            if not _is_bundle_root(root)
        ]
        return state
    if isinstance(value, OperationError):
        return {"code": value.code, "message": value.message, "params": _encode(value.params)}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported management result type: {type(value).__name__}")


def _bundle_by_asset(store: ExtensionInstallState, asset_id: str) -> Any:
    """Return the installed container published under ``asset_id``."""

    if not asset_id:
        return None
    for record in store.bundles():
        if record.asset_id == asset_id:
            return record
    return None


def _agentserver_query_result(state: QueryResult) -> QueryResult:
    """Project the shared Core report onto the AgentServer-owned domain."""

    packages = tuple(record for record in state.packages if record.domain == DOMAIN_AGENT_SERVER)
    domain_ids = {record.package_id for record in state.active}
    domain_ids.update(record.package_id for record in packages)
    return replace(
        state,
        inflight={
            package_id: count
            for package_id, count in state.inflight.items()
            if package_id in domain_ids
        },
        published={
            package_id: handlers
            for package_id, handlers in state.published.items()
            if package_id in domain_ids
        },
        packages=packages,
    )


def _installed_package_domain(root: Path) -> str | None:
    """Read one installed package domain for the slash-command presentation."""
    try:
        return package_domain(read_manifest_document(root))
    except (OSError, ValueError):
        return None


def _is_bundle_root(root: Path) -> bool:
    """Bundle roots share the store layout but are not standalone packages."""

    try:
        return is_bundle_document(read_manifest_document(root))
    except (OSError, ValueError):
        return False
