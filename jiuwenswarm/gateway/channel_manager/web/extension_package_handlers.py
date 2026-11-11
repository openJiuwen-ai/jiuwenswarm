# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Route V2 package-management requests to the Gateway or the AgentServer.

Gateway and AgentServer share ``extension_pkgs``; a request belongs to the
Gateway only when every addressed source declares ``package.domain: gateway``. Anything
else keeps the previous behaviour: the frame is proxied to the owning
AgentServer unchanged. Management frames never enter the extension bus, so an
extension cannot intercept its own uninstall.

Package file listing and reading always go to the AgentServer, which owns
package source resolution for both domains.

The authentication gate applies to every inbound frame, including management
frames: a request without an ``authorization`` header is answered with
``AUTH_REQUIRED`` by the BUILTIN gate before it reaches this module. Before a
login-owning package is published the gate stays inert, so management remains
reachable and the first login package can be installed.
"""

from __future__ import annotations

import json
import logging
import tarfile
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from tarfile import ReadError, TarError
from typing import Any
from zipfile import BadZipFile, ZipFile

from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.package_manager import PackageManager
from openjiuwen.core.extension.versions import parse_version

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.extensions_v2.config import resolve_examples_dir
from jiuwenswarm.gateway.extensions_v2.manifest import (
    DOMAIN_AGENT_SERVER,
    DOMAIN_GATEWAY,
)
from jiuwenswarm.gateway.extensions_v2.management.catalog import (
    PackageNotFound,
    list_installed,
    list_presets,
    package_detail,
    resolve_install_source,
)
from jiuwenswarm.gateway.extensions_v2.management.notifications import notify_installed
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary, proxy_unary_request
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionInstallRecord,
    ExtensionInstallState,
)
from jiuwenswarm.server.runtime.marketplace.hub_extensions import (
    download_system_plugin_archive,
    query_system_plugin,
    system_plugin_domain,
)

MANAGEMENT_METHODS = frozenset(
    {
        ReqMethod.EXTENSIONS_V2_QUERY.value,
        ReqMethod.EXTENSIONS_V2_CATALOG.value,
        ReqMethod.EXTENSIONS_V2_SHOW.value,
        ReqMethod.EXTENSIONS_V2_IMPORT.value,
        ReqMethod.EXTENSIONS_V2_INSTALL.value,
        ReqMethod.EXTENSIONS_V2_UNINSTALL.value,
        ReqMethod.EXTENSIONS_V2_RELOAD.value,
        ReqMethod.EXTENSIONS_V2_SET_ENABLED.value,
    }
)
INVALID_PARAMS_CODE = "INVALID_PARAMS"
OPERATION_FAILED_CODE = "EXTENSION_OPERATION_FAILED"

logger = logging.getLogger(__name__)


def register_extension_package_handlers(channel: Any, resolve_client: Callable[[], Any]) -> None:
    """Register the domain split; the Gateway still forwards agent packages."""

    def create_handler(method: ReqMethod) -> Callable[..., Awaitable[None]]:
        async def handler(
            ws: Any,
            req_id: str,
            params: dict[str, Any],
            session_id: str | None,
            user_id: str | None = None,
        ) -> None:
            if await _handle_locally(
                channel=channel,
                resolve_client=resolve_client,
                ws=ws,
                req_id=req_id,
                params=params,
                session_id=session_id,
                user_id=user_id,
                method=method,
            ):
                return
            await proxy_unary_request(
                channel=channel,
                agent_client=resolve_client(),
                ws=ws,
                req_id=req_id,
                params=params,
                session_id=session_id,
                user_id=user_id,
                req_method=method,
                label=method.value,
                timeout_seconds=_request_timeout(),
                drop_params=("session_id",),
                preserve_error_payload=True,
            )

        return handler

    for method in (
        ReqMethod.EXTENSIONS_V2_QUERY,
        ReqMethod.EXTENSIONS_V2_CATALOG,
        ReqMethod.EXTENSIONS_V2_SHOW,
        ReqMethod.EXTENSIONS_V2_IMPORT,
        ReqMethod.EXTENSIONS_V2_INSTALL,
        ReqMethod.EXTENSIONS_V2_UNINSTALL,
        ReqMethod.EXTENSIONS_V2_RELOAD,
        ReqMethod.EXTENSIONS_V2_SET_ENABLED,
        ReqMethod.EXTENSIONS_V2_FILE_LIST,
        ReqMethod.EXTENSIONS_V2_FILE_READ,
    ):
        channel.register_method(method.value, create_handler(method))


async def _handle_locally(
    *,
    channel: Any,
    resolve_client: Callable[[], Any],
    ws: Any,
    req_id: str,
    params: Mapping[str, Any],
    session_id: str | None,
    user_id: str | None,
    method: ReqMethod,
) -> bool:
    """Return True when the Gateway answered the frame itself."""

    from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime

    if method.value not in MANAGEMENT_METHODS or not isinstance(params, Mapping):
        return False
    runtime = get_gateway_runtime()
    enabled = runtime is not None and runtime.config.enabled
    manager = runtime.packages if enabled else None

    if method is ReqMethod.EXTENSIONS_V2_IMPORT:
        await _import_local(
            channel=channel,
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            runtime=runtime if enabled else None,
            resolve_client=resolve_client,
        )
        return True

    if manager is None:
        return False

    if method is ReqMethod.EXTENSIONS_V2_QUERY:
        package_id = params.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            # The shared report is owned by AgentServer; it returns every domain id.
            return False
        if await manager.get_package_domain(package_id) != DOMAIN_GATEWAY:
            return False
        await _respond(channel, ws, req_id, ok=True, payload={"state": manager.query(package_id)})
        return True

    if method is ReqMethod.EXTENSIONS_V2_CATALOG:
        scope = params.get("scope", "catalog")
        if scope not in {"catalog", "mine"}:
            return False
        examples_dir = resolve_examples_dir()
        state = manager.query()
        if scope == "mine":
            packages = list_installed(examples_dir, state)
        else:
            packages = list_presets(examples_dir, state)
        cache = None
        agent_client = resolve_client()
        if agent_client is not None:
            ok, payload = await fetch_agent_unary(
                agent_client=agent_client,
                req_method=method,
                params=dict(params),
                session_id=session_id,
                user_id=user_id,
                channel_id=channel.channel_id,
                label=method.value,
                timeout_seconds=_request_timeout(),
            )
            remote = payload.get("packages") if ok else None
            if isinstance(remote, list):
                packages = _merge_packages(packages, remote, state)
                cached = payload.get("cache")
                cache = dict(cached) if ok and isinstance(cached, Mapping) else None
        response: dict[str, Any] = {"packages": packages}
        if cache is not None:
            response["cache"] = cache
        await _respond(channel, ws, req_id, ok=True, payload=response)
        return True

    if method is ReqMethod.EXTENSIONS_V2_SHOW:
        package_id = params.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            return False
        examples_dir = resolve_examples_dir()
        store = ExtensionInstallState.for_install_dir(manager.install_dir)
        record = store.get(package_id)
        requested = params.get("version")
        requested = requested.strip() if isinstance(requested, str) else ""
        if (
            record is not None
            and record.origin == "hub"
            and record.asset_id
            and requested
            and requested != record.version
        ):
            # Another Hub version is not installed locally; AgentServer serves it.
            return False
        try:
            detail = package_detail(package_id, examples_dir, manager.query())
        except PackageNotFound:
            return False
        if record is not None and record.origin == "hub" and record.asset_id:
            try:
                hub = await query_system_plugin(record.asset_id)
                detail["versions"] = _merge_versions(
                    detail.get("versions") or [],
                    list(hub.versions) or [detail.get("installedVersion") or detail["version"]],
                )
                detail["preview"] = detail.get("preview") or hub.preview_url or None
            except Exception:  # noqa: BLE001 - installed details must survive Hub outages
                logger.warning("[GatewayExtensions] Hub detail unavailable for %s", record.asset_id, exc_info=True)
        await _respond(channel, ws, req_id, ok=True, payload={"package": detail})
        return True

    if method is ReqMethod.EXTENSIONS_V2_INSTALL:
        examples_dir = resolve_examples_dir()
        store = ExtensionInstallState.for_install_dir(manager.install_dir)
        version = params.get("version")
        version = version.strip() if isinstance(version, str) else ""
        hub_tmp = None
        hub_asset_id = ""
        if "package_ids" in params:
            ids = params.get("package_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
                await _respond(
                    channel,
                    ws,
                    req_id,
                    ok=False,
                    error="package_ids must be a nonempty array of string ids",
                    code=INVALID_PARAMS_CODE,
                )
                return True
            try:
                paths = []
                installed_asset_id = ""
                for package_id in ids:
                    installed_record = store.get(package_id)
                    if (
                        installed_record is not None
                        and installed_record.origin == "hub"
                        and installed_record.asset_id
                    ):
                        installed_asset_id = installed_record.asset_id
                        raise PackageNotFound(package_id)
                    paths.append(
                        resolve_install_source(
                            package_id,
                            examples_dir=examples_dir,
                            install_dir=manager.install_dir,
                        )
                    )
            except PackageNotFound as exc:
                if len(ids) != 1:
                    await _respond(
                        channel, ws, req_id, ok=False, error=f"package {exc} not found", code="PACKAGE_NOT_FOUND"
                    )
                    return True
                try:
                    detail = await query_system_plugin(installed_asset_id or ids[0], version)
                except Exception:  # noqa: BLE001 - a missing Hub asset falls back to AgentServer routing
                    return False
                if system_plugin_domain(detail) != DOMAIN_GATEWAY:
                    return False
                hub_asset_id = detail.asset_id
                package_id = detail.package_name or ids[0]
                stored_root = manager.install_dir / package_id / detail.version
                if stored_root.is_dir():
                    paths = [stored_root]
                else:
                    hub_tmp = tempfile.TemporaryDirectory(prefix="ojw_gateway_plugin_")
                    archive = Path(hub_tmp.name) / f"{package_id}-{detail.version}.zip"
                    try:
                        await download_system_plugin_archive(detail, archive)
                    except Exception:  # noqa: BLE001 - download failure is reported by the proxy path
                        hub_tmp.cleanup()
                        return False
                    paths = [archive]
            except ValueError:
                await _respond(
                    channel, ws, req_id, ok=False, error="package_ids contains an invalid id", code=INVALID_PARAMS_CODE
                )
                return True
        else:
            paths, error = _install_paths(params)
            if error is not None:
                await _respond(channel, ws, req_id, ok=False, error=error, code=INVALID_PARAMS_CODE)
                return True
        owners: list[bool] = []
        for path in paths:
            try:
                owners.append(await manager.get_source_domain(path) == DOMAIN_GATEWAY)
            except Exception:  # noqa: BLE001 - an unreadable source belongs to neither side
                await _respond(
                    channel,
                    ws,
                    req_id,
                    ok=False,
                    error="install source is not a valid package",
                    code=INVALID_PARAMS_CODE,
                )
                return True
        if not any(owners):
            if hub_tmp is not None:
                hub_tmp.cleanup()
            return False
        if not all(owners):
            if hub_tmp is not None:
                hub_tmp.cleanup()
            await _respond(
                channel,
                ws,
                req_id,
                ok=False,
                error="gateway and agent packages cannot be installed in one request",
                code=INVALID_PARAMS_CODE,
            )
            return True
        try:
            if "package_ids" in params:
                reports = []
                for path in paths:
                    package_id = _manifest_package_id(path)
                    manifest_version = _manifest_version(path)
                    target_version = version or manifest_version
                    if not package_id or not target_version:
                        await _respond(
                            channel,
                            ws,
                            req_id,
                            ok=False,
                            error="install source is not a valid package",
                            code=INVALID_PARAMS_CODE,
                        )
                        return True
                    stored_root = manager.install_dir / package_id / target_version
                    if not stored_root.is_dir() and manifest_version != target_version:
                        await _respond(
                            channel,
                            ws,
                            req_id,
                            ok=False,
                            error=f"package {package_id} version {target_version} not found",
                            code="PACKAGE_NOT_FOUND",
                        )
                        return True
                    reports.append(
                        await manager.enable_version(
                            package_id,
                            target_version,
                            source=None if stored_root.is_dir() else path,
                        )
                    )
            else:
                reports = await manager.install(paths)
        finally:
            if hub_tmp is not None:
                hub_tmp.cleanup()
        origin = (
            _install_origin(paths, examples_dir, store, hub_asset_id)
            if "package_ids" in params
            else "user"
        )
        _record_installed(reports, manager, store, origin=origin, asset_id=hub_asset_id)
        ui_notified = await notify_installed(runtime, [report.package_id for report in reports if report.status == "SUCCESS"])
        await _respond(
            channel,
            ws,
            req_id,
            ok=True,
            payload={
                "reports": [_encode(report) for report in reports],
                "installed_packages": manager.query()["installed_packages"],
                "ui_notified": ui_notified,
            },
        )
        return True

    package_id = params.get("package_id")
    if not isinstance(package_id, str) or not package_id:
        return False
    domain = await manager.get_package_domain(package_id)
    if domain != DOMAIN_GATEWAY and not (domain is None and manager.owns(package_id)):
        return False
    if method is ReqMethod.EXTENSIONS_V2_SET_ENABLED:
        if not isinstance(params.get("enable"), bool):
            await _respond(
                channel,
                ws,
                req_id,
                ok=False,
                error="set enabled requires package_id and boolean enable",
                code=INVALID_PARAMS_CODE,
            )
            return True
        report = await manager.set_enabled(package_id, params["enable"])
        payload = {"reports": [_encode(report)]}
        if report.status == "SUCCESS":
            store = ExtensionInstallState.for_install_dir(manager.install_dir)
            if params["enable"]:
                version = _active_version(manager, package_id)
                if version:
                    store.mark(package_id, version=version, active=True, enabled=True)
                payload["ui_notified"] = await notify_installed(runtime, [package_id])
            else:
                store.mark(package_id, active=False, enabled=False)
        await _respond(channel, ws, req_id, ok=True, payload=payload)
        return True
    version = params.get("version")
    version = version.strip() if isinstance(version, str) else ""
    try:
        if method is ReqMethod.EXTENSIONS_V2_UNINSTALL:
            report = (
                await manager.uninstall(package_id, version)
                if version
                else await manager.uninstall(package_id)
            )
        else:
            report = await manager.reload(package_id)
    except Exception as exc:  # noqa: BLE001 - answer the frame instead of dropping it
        logger.exception("[GatewayExtensions] %s failed for %s", method.value, package_id)
        await _respond(
            channel,
            ws,
            req_id,
            ok=False,
            error=f"{method.value} failed: {exc}",
            code=OPERATION_FAILED_CODE,
        )
        return True
    payload = {"reports": [_encode(report)]}
    if report.status == "SUCCESS":
        store = ExtensionInstallState.for_install_dir(manager.install_dir)
        if method is ReqMethod.EXTENSIONS_V2_UNINSTALL:
            store.remove(package_id, version)
        else:
            version = _active_version(manager, package_id)
            if version:
                store.mark(package_id, version=version, active=True, enabled=True)
    if method is ReqMethod.EXTENSIONS_V2_RELOAD and report.status == "SUCCESS":
        payload["ui_notified"] = await notify_installed(runtime, [report.package_id])
    await _respond(channel, ws, req_id, ok=True, payload=payload)
    return True


def _active_version(manager: Any, package_id: str) -> str:
    active = manager.query(package_id).get("active") or []
    return str(active[0].get("version") or "") if active else ""


async def _import_local(
    *,
    channel: Any,
    ws: Any,
    req_id: str,
    params: Mapping[str, Any],
    session_id: str | None,
    user_id: str | None,
    runtime: Any | None,
    resolve_client: Callable[[], Any],
) -> None:
    """Install one local package as a Gateway extension, an Agent extension or an Agent plugin."""

    raw = params.get("path")
    if not isinstance(raw, str) or not raw.strip():
        await _respond(channel, ws, req_id, ok=False, error="path must be a nonempty string", code=INVALID_PARAMS_CODE)
        return
    path = Path(raw.strip())
    if not path.is_absolute() or not path.exists():
        await _respond(
            channel, ws, req_id, ok=False, error="import requires an existing absolute path", code=INVALID_PARAMS_CODE
        )
        return

    domain = _package_domain(path)
    if domain is None:
        await _respond(
            channel,
            ws,
            req_id,
            ok=False,
            error="manifest.json is missing or declares an unsupported package_type or domain",
            code=INVALID_PARAMS_CODE,
        )
        return
    if domain == DOMAIN_GATEWAY:
        if runtime is None:
            await _respond(
                channel, ws, req_id, ok=False, error="gateway extensions are disabled", code=INVALID_PARAMS_CODE
            )
            return
        reports = await runtime.packages.install([path])
        failed = next((report for report in reports if report.status != "SUCCESS"), None)
        if failed is not None:
            message = getattr(getattr(failed, "error", None), "message", None) or "package install failed"
            await _respond(channel, ws, req_id, ok=False, error=str(message), code="INSTALL_FAILED")
            return
        _record_installed(
            reports,
            runtime.packages,
            ExtensionInstallState.for_install_dir(runtime.packages.install_dir),
            origin="user",
        )
        ui_notified = await notify_installed(runtime, [report.package_id for report in reports])
        await _respond(
            channel,
            ws,
            req_id,
            ok=True,
            payload={"kind": "extension", "reports": [_encode(report) for report in reports], "ui_notified": ui_notified},
        )
        return

    method = ReqMethod.EXTENSIONS_V2_INSTALL if domain == DOMAIN_AGENT_SERVER else ReqMethod.PLUGIN_PACKAGES_IMPORT_LOCAL
    forwarded = {"paths": [str(path)]} if domain == DOMAIN_AGENT_SERVER else {"path": str(path)}
    ok, payload = await fetch_agent_unary(
        agent_client=resolve_client(),
        req_method=method,
        params=forwarded,
        session_id=session_id,
        user_id=user_id,
        channel_id=channel.channel_id,
        label=method.value,
        timeout_seconds=_request_timeout(),
    )
    if not ok:
        await _respond(
            channel,
            ws,
            req_id,
            ok=False,
            error=str(payload.get("error") or "package install failed"),
            code=str(payload.get("code") or "INSTALL_FAILED"),
        )
        return
    kind = "extension" if domain == DOMAIN_AGENT_SERVER else "plugin"
    await _respond(channel, ws, req_id, ok=True, payload={**payload, "kind": kind})


_PACKAGE_TYPES = {"plugin": "plugin", "extension": DOMAIN_AGENT_SERVER}
_KNOWN_DOMAINS = frozenset({DOMAIN_AGENT_SERVER, DOMAIN_GATEWAY})
_MANIFEST_LIMIT = 64 * 1024


def _package_domain(path: Path) -> str | None:
    """Classify a source by ``package.domain`` and ``package_type`` without importing it."""

    domain = _extension_source_domain(path)
    if domain is not None:
        return domain
    manifest = _read_manifest(path)
    if manifest is None:
        return None
    package = manifest.get("package")
    domain = package.get("domain") if isinstance(package, Mapping) else None
    if domain is not None:
        value = domain.strip().lower() if isinstance(domain, str) else ""
        return value if value in _KNOWN_DOMAINS else None
    package_type = manifest.get("package_type")
    return _PACKAGE_TYPES.get(package_type) if isinstance(package_type, str) else None


def _extension_source_domain(path: Path) -> str | None:
    """Read an extension source with Core's ZIP-root rules before routing it."""

    try:
        with PackageManager(path.parent).open_source(path) as opened:
            domain = (opened.manifest.domain or "").strip().lower() or DOMAIN_AGENT_SERVER
    except ExtensionFailure:
        return None
    return domain if domain in _KNOWN_DOMAINS else None


def _read_manifest(path: Path) -> Mapping[str, Any] | None:
    """Read ``manifest.json`` from a directory or zip source."""

    try:
        if path.is_dir():
            raw = (path / "manifest.json").read_bytes()
        elif any(str(path).lower().endswith(suffix) for suffix in (".tar", ".tar.gz", ".tgz")):
            with tarfile.open(path, "r:*") as archive:
                entry = archive.getmember("manifest.json")
                if entry.size > _MANIFEST_LIMIT:
                    return None
                stream = archive.extractfile(entry)
                raw = stream.read() if stream is not None else b""
        else:
            with ZipFile(path) as archive:
                candidates = [
                    entry
                    for entry in archive.infolist()
                    if Path(entry.filename).name == "manifest.json"
                    and len(Path(entry.filename).parts) <= 3
                ]
                if len(candidates) != 1:
                    return None
                entry = candidates[0]
                if entry.file_size > _MANIFEST_LIMIT:
                    return None
                raw = archive.read(entry)
        document = json.loads(raw)
    except (OSError, KeyError, BadZipFile, ReadError, TarError, UnicodeError, ValueError):
        return None
    return document if isinstance(document, Mapping) else None


def _manifest_version(source: Path) -> str:
    document = _read_manifest(source)
    package = document.get("package") if document is not None else None
    version = package.get("version") if isinstance(package, Mapping) else None
    return version.strip() if isinstance(version, str) else ""


def _manifest_package_id(source: Path) -> str:
    document = _read_manifest(source)
    package = document.get("package") if document is not None else None
    package_id = package.get("id") if isinstance(package, Mapping) else None
    return package_id.strip() if isinstance(package_id, str) else ""


def _installed_version(state: Mapping[str, Any], package_id: str) -> str:
    for record in state.get("active") or []:
        if isinstance(record, Mapping) and record.get("package_id") == package_id:
            return str(record.get("version") or "")
    versions = [
        str(item.get("version") or "")
        for item in state.get("installed_packages") or []
        if isinstance(item, Mapping) and item.get("package_id") == package_id
    ]
    return max(versions, key=_version_key, default="")


def _version_key(version: str) -> tuple[int, int, int]:
    try:
        parsed = parse_version(version)
    except (TypeError, ValueError):
        return (0, 0, 0)
    return (parsed.major, parsed.minor, parsed.patch)


def _merge_versions(*groups: list[str]) -> list[str]:
    return sorted({version for group in groups for version in group if version}, key=_version_key)


def _record_installed(
    reports: list[Any],
    manager: Any,
    store: ExtensionInstallState,
    *,
    origin: str,
    asset_id: str = "",
) -> None:
    state = manager.query()
    active_versions = {
        str(item.get("package_id")): str(item.get("version") or "")
        for item in state.get("active") or []
        if isinstance(item, Mapping) and item.get("package_id")
    }
    installed = [
        item for item in state.get("installed_packages") or [] if isinstance(item, Mapping)
    ]
    for report in reports:
        if report.status != "SUCCESS":
            continue
        version = active_versions.get(report.package_id) or _installed_version(state, report.package_id)
        facts = next(
            (
                item for item in installed
                if item.get("package_id") == report.package_id and item.get("version") == version
            ),
            {},
        )
        if not version:
            continue
        record = ExtensionInstallRecord(
            package_id=report.package_id,
            version=version,
            origin=origin,
            asset_id=asset_id,
            install_path=str(facts.get("install_path") or ""),
            active=active_versions.get(report.package_id) == version,
            enabled=active_versions.get(report.package_id) == version,
        )
        if record.active:
            store.activate(record)
        else:
            store.mark(report.package_id, active=False, enabled=False)
            store.upsert(record)


def _install_paths(params: Mapping[str, Any]) -> tuple[list[Path], str | None]:
    raw = params.get("paths")
    if not isinstance(raw, list) or not raw:
        return [], "paths must be a nonempty array of absolute ZIP paths"
    paths: list[Path] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return [], "each path must be a nonempty string"
        path = Path(item)
        if not path.is_absolute() or path.suffix.lower() != ".zip":
            return [], "install accepts existing .zip files only"
        paths.append(path)
    return paths, None


def _merge_packages(
    local: list[dict[str, Any]],
    remote: list[Any],
    state: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Merge Gateway cards into the AgentServer catalog, Gateway first."""

    installed = {
        str(item.get("package_id"))
        for item in (state or {}).get("installed_packages") or []
        if isinstance(item, Mapping) and item.get("package_id")
    }
    active = {
        str(item.get("package_id"))
        for item in (state or {}).get("active") or []
        if isinstance(item, Mapping) and item.get("package_id")
    }
    merged: dict[str, dict[str, Any]] = {}
    identities: set[str] = set()
    for item in [*local, *(entry for entry in remote if isinstance(entry, Mapping))]:
        identity = str(item.get("packageId") or item.get("packageName") or item.get("id") or "")
        if not identity or identity in identities:
            continue
        identities.add(identity)
        card = dict(item)
        package_id = str(card.get("packageId") or "")
        if package_id in installed:
            card["installed"] = True
        if package_id in active:
            card["active"] = True
            card["enabled"] = True
        merged[str(card.get("id") or identity)] = card
    return list(merged.values())


def _install_origin(
    paths: list[Path],
    examples_dir: Path,
    store: ExtensionInstallState,
    hub_asset_id: str,
) -> str:
    """Keep the recorded source when a version switch reuses a stored package."""

    if hub_asset_id:
        return "hub"
    for path in paths:
        record = store.get(_manifest_package_id(path))
        if record is not None:
            return record.origin
    examples = examples_dir.resolve()
    for path in paths:
        try:
            if not path.resolve().is_relative_to(examples):
                return "user"
        except OSError:
            return "user"
    return "preset"


def _request_timeout() -> float:
    """Client and proxy wait at least the dependency window plus one margin."""

    from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime

    runtime = get_gateway_runtime()
    if runtime is None:
        return 330.0
    return float(runtime.config.dependency_timeout) + 30.0


def _encode(value: object) -> Any:
    """Encode Core report objects into their wire shape."""

    if is_dataclass(value) and not isinstance(value, type):
        result = {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
        error = getattr(value, "error", None)
        if error is not None:
            result["error"] = {
                "code": error.code,
                "message": error.message,
                "params": _encode(error.params),
            }
        return result
    if isinstance(value, Mapping):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


async def _respond(
    channel: Any,
    ws: Any,
    req_id: str,
    *,
    ok: bool,
    payload: Mapping[str, Any] | None = None,
    error: str | None = None,
    code: str | None = None,
) -> None:
    await channel.send_response(
        ws,
        req_id,
        ok=ok,
        payload=dict(payload or {}),
        error=error,
        code=code,
    )
