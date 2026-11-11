# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer requests for the already assembled V2 package manager."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, ClassVar

from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.data.json_values import valid_package_id
from openjiuwen.core.extension.errors import OperationError
from openjiuwen.core.extension.management import QueryResult

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.assembly import get_extension_manager
from jiuwenswarm.extensions_v2.config import resolve_examples_dir, resolve_install_dir
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
    package_domain,
    read_manifest_document,
)
from jiuwenswarm.server.runtime.extension_package_manager import (
    list_package_files,
    read_package_file,
)
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionInstallRecord,
    ExtensionInstallState,
)
from jiuwenswarm.server.runtime.marketplace.hub_extensions import download_system_plugin_archive
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)

_PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class _Request:
    paths: tuple[Path, ...] = ()
    package_ids: tuple[str, ...] = ()
    package_id: str = ""
    path: str = ""
    scope: str = "catalog"
    enable: bool | None = None
    version: str = ""


class PackageManagementAdapter(GatewayAdapter):
    """Validate management requests without reimplementing the Core lifecycle."""

    methods: ClassVar[frozenset[str]] = frozenset(
        {
            ReqMethod.EXTENSIONS_V2_QUERY.value,
            ReqMethod.EXTENSIONS_V2_CATALOG.value,
            ReqMethod.EXTENSIONS_V2_SHOW.value,
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
            payload = await _dispatch(manager, method, parsed)
        except PackageNotFound as exc:
            return _error_response(request, f"package {exc} not found", code=_PACKAGE_NOT_FOUND)
        except ValueError as exc:
            return _error_response(request, str(exc), code="INVALID_PARAMS")
        except ExtensionFailure as exc:
            return await _failed_request(request, manager, parsed.package_id, exc.message, code=exc.status.name)
        except Exception:  # noqa: BLE001 - keep management failures at the request boundary
            logger.exception("[ExtensionsV2] management request failed: %s", method)
            return await _failed_request(request, manager, parsed.package_id, "V2 package management failed")
        if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
            payload["ui_notified"] = await _notify_installed_packages(payload.get("reports", ()))
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


async def _dispatch(manager: ExtensionManager, method: str, parsed: _Request) -> dict[str, Any]:
    if method == ReqMethod.EXTENSIONS_V2_CATALOG.value:
        state = await manager.query()
        examples_dir = resolve_examples_dir()
        install_dir = resolve_install_dir()
        packages = (
            list_catalog(examples_dir, state, domain=DOMAIN_AGENT_SERVER, install_dir=install_dir)
            if parsed.scope == "catalog"
            else list_mine(examples_dir, state, domain=DOMAIN_AGENT_SERVER, install_dir=install_dir)
        )
        if parsed.scope == "catalog":
            local_ids = {str(item.get("id")) for item in packages}
            hub_packages = await list_hub_system_plugins(state, install_dir)
            packages.extend(
                item
                for item in hub_packages
                if item["id"] not in local_ids and item.get("packageName") not in local_ids
            )
        return {"packages": packages}
    if method == ReqMethod.EXTENSIONS_V2_SHOW.value:
        state = await manager.query()
        install_dir = resolve_install_dir()
        store = ExtensionInstallState.for_install_dir(install_dir)
        try:
            entry = find_entry(
                parsed.package_id,
                resolve_examples_dir(),
                state,
                domain=DOMAIN_AGENT_SERVER,
                install_dir=install_dir,
            )
        except PackageNotFound:
            record = store.get(parsed.package_id) or store.by_asset(parsed.package_id)
            hub_key = record.asset_id if record is not None and record.asset_id else parsed.package_id
            detail = await get_hub_system_plugin(hub_key, parsed.version)
            return {"package": hub_detail(detail, state, store)}
        detail = project_detail(entry, state, install_dir=install_dir)
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
        record = store.get(parsed.package_id) or store.by_asset(parsed.package_id)
        package_id = record.package_id if record is not None else parsed.package_id
        installed = installed_version(build_state_index(state), package_id) or (
            record.version if record is not None else ""
        )
        record = store.get(package_id, installed) or record
        if record is not None and record.origin == "hub" and parsed.version and parsed.version != installed:
            return await _hub_file_payload(
                record.asset_id or package_id, method, parsed.path, parsed.version
            )
        try:
            entry = find_entry(
                package_id,
                resolve_examples_dir(),
                state,
                install_dir=install_dir,
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
    if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
        install_dir = resolve_install_dir()
        store = ExtensionInstallState.for_install_dir(install_dir)
        reports: list[ActivationReport] = []
        if parsed.paths:
            installed_reports = list(await manager.install(list(parsed.paths)))
            _record_reports(installed_reports, manager, store, origin="user")
            reports.extend(installed_reports)
        if parsed.package_ids:
            reports.extend(
                await _install_package_ids(manager, store, parsed, install_dir)
            )
        return {"reports": reports, "installed_packages": _installed_packages(manager, reports)}
    if method == ReqMethod.EXTENSIONS_V2_UNINSTALL.value:
        report = await (
            manager.uninstall(parsed.package_id, parsed.version)
            if parsed.version
            else manager.uninstall(parsed.package_id)
        )
        if report.status == "SUCCESS":
            ExtensionInstallState.for_install_dir(resolve_install_dir()).remove(
                parsed.package_id, parsed.version
            )
        return {"reports": [report]}
    if method == ReqMethod.EXTENSIONS_V2_RELOAD.value:
        report = await manager.reload(parsed.package_id)
        if report.status == "SUCCESS":
            store = ExtensionInstallState.for_install_dir(resolve_install_dir())
            version = _active_version(manager, parsed.package_id)
            if version:
                store.mark(parsed.package_id, version=version, active=True, enabled=True)
        return {"reports": [report]}
    if method == ReqMethod.EXTENSIONS_V2_SET_ENABLED.value:
        report = await manager.set_enabled(parsed.package_id, parsed.enable is True)
        if report.status == "SUCCESS":
            store = ExtensionInstallState.for_install_dir(resolve_install_dir())
            if parsed.enable is True:
                version = _active_version(manager, parsed.package_id)
                if version:
                    store.mark(parsed.package_id, version=version, active=True, enabled=True)
            else:
                store.mark(parsed.package_id, active=False, enabled=False)
        return {"reports": [report]}
    return {"state": await manager.query(parsed.package_id or None)}


async def _install_package_ids(
    manager: ExtensionManager,
    store: ExtensionInstallState,
    parsed: _Request,
    install_dir: Path,
) -> list[ActivationReport]:
    """Activate one version per package while keeping other stored versions on disk."""

    examples_dir = resolve_examples_dir()
    state = await manager.query()
    index = build_state_index(state)
    reports: list[ActivationReport] = []
    for requested_id in parsed.package_ids:
        report = await _activate_requested_version(
            manager,
            store,
            parsed,
            install_dir,
            examples_dir,
            index,
            requested_id,
        )
        if report is not None:
            reports.append(report)
    return reports


async def _activate_requested_version(
    manager: ExtensionManager,
    store: ExtensionInstallState,
    parsed: _Request,
    install_dir: Path,
    examples_dir: Path,
    index: StateIndex,
    requested_id: str,
) -> ActivationReport | None:
    """Resolve the requested version, activating a stored copy when one exists."""

    current = store.get(requested_id) or store.by_asset(requested_id)
    source: Path | None = None
    local_entry = None
    try:
        source = resolve_install_source(requested_id, examples_dir=examples_dir, install_dir=install_dir)
        local_entry = entry_from_root(source)
    except PackageNotFound:
        pass

    package_id = local_entry.package_id if local_entry is not None else (
        current.package_id if current is not None else requested_id
    )
    prefer_hub = current is not None and current.origin == "hub"
    desired = parsed.version or (
        local_entry.version if local_entry is not None and not prefer_hub else ""
    )
    installed = installed_version(index, package_id)
    if not desired:
        if installed:
            return None
        detail = await get_hub_system_plugin(requested_id, "")
        package_id = detail.package_name or package_id
        desired = detail.version
    if not desired:
        raise ValueError(f"package {requested_id} has no version to install")

    target_record = store.get(package_id, desired)
    origin = target_record.origin if target_record is not None else (
        current.origin if current is not None else "preset"
    )
    asset_id = target_record.asset_id if target_record is not None else (
        current.asset_id if current is not None else ""
    )
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if not _has_stored_version(manager, package_id, desired):
            if local_entry is not None and local_entry.version == desired and not prefer_hub:
                origin = "preset"
                asset_id = ""
            else:
                detail = await get_hub_system_plugin(asset_id or requested_id, desired)
                package_id = detail.package_name or package_id
                desired = detail.version
                target_record = store.get(package_id, desired)
                if target_record is not None:
                    origin = target_record.origin
                    asset_id = target_record.asset_id
                if not _has_stored_version(manager, package_id, desired):
                    temporary = tempfile.TemporaryDirectory(prefix="ojw_system_plugin_")
                    archive = Path(temporary.name) / f"{package_id}-{desired}.zip"
                    await download_system_plugin_archive(detail, archive)
                    source = archive
                    origin = "hub"
                    asset_id = detail.asset_id
        report = await manager.enable_version(package_id, desired, source=source)
    finally:
        if temporary is not None:
            temporary.cleanup()

    if report.status == "SUCCESS":
        _record_reports([report], manager, store, origin=origin, asset_id=asset_id)
    return report


def _has_stored_version(manager: ExtensionManager, package_id: str, version: str) -> bool:
    return bool(package_id and version) and (manager.install_dir / package_id / version).is_dir()


def _active_version(manager: ExtensionManager, package_id: str) -> str:
    for record in manager.active_packages():
        if record.package_id == package_id:
            return record.version
    return ""


def _record_reports(
    reports: Sequence[ActivationReport],
    manager: ExtensionManager,
    store: ExtensionInstallState,
    *,
    origin: str,
    asset_id: str = "",
) -> None:
    installed = {
        str(item["package_id"]): item
        for item in _installed_packages(manager, reports)
    }
    for report in reports:
        if report.status != "SUCCESS":
            continue
        facts = installed.get(report.package_id, {})
        version = str(facts.get("version") or "")
        if not version:
            continue
        store.activate(
            ExtensionInstallRecord(
                package_id=report.package_id,
                version=version,
                origin=origin,
                asset_id=asset_id,
                install_path=str(facts.get("install_path") or ""),
                active=True,
                enabled=True,
            )
        )


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


async def _notify_installed_packages(reports: Sequence[ActivationReport]) -> bool:
    """Refresh mounted WebUI slots after a confirmed install."""
    succeeded = [report.package_id for report in reports if report.status == "SUCCESS"]
    if not succeeded:
        return False
    from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

    try:
        runtime = get_webui_runtime()
        return await runtime.notify_packages_changed(succeeded) if runtime is not None else False
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


def _parse_request(method: object, params: object) -> _Request:
    if not isinstance(method, str) or method not in PackageManagementAdapter.methods:
        raise ValueError("unsupported V2 package management method")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

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
        if set(params) - {"scope"}:
            raise ValueError("catalog accepts scope only")
        scope = params.get("scope", "catalog")
        if scope not in {"catalog", "mine"}:
            raise ValueError("scope must be 'catalog' or 'mine'")
        return _Request(scope=scope)

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
        if set(params) != {"package_id", "enable"}:
            raise ValueError("set enabled requires package_id and enable")
        enable = params.get("enable")
        if not isinstance(enable, bool):
            raise ValueError("enable must be a boolean")
        return _Request(package_id=_package_id(params["package_id"]), enable=enable)

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
    if len(version) > 64 or any(ch.isspace() for ch in version) or "/" in version or "\\" in version:
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
            extra["state"] = _encode(state)
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


def _installed_package_domain(root: Path) -> str | None:
    """Read one installed package domain for the slash-command presentation."""
    try:
        return package_domain(read_manifest_document(root))
    except (OSError, UnicodeError, ValueError):
        return None
