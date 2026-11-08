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
import tarfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from tarfile import ReadError, TarError
from typing import Any
from zipfile import BadZipFile, ZipFile

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.extensions_v2.config import resolve_examples_dir
from jiuwenswarm.gateway.extensions_v2.manifest import DOMAIN_AGENT_SERVER, DOMAIN_GATEWAY
from jiuwenswarm.gateway.extensions_v2.management.catalog import (
    PackageNotFound,
    list_installed,
    list_presets,
    package_detail,
    resolve_install_source,
)
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary, proxy_unary_request
from jiuwenswarm.common.extension_cli import merge_query_states

MANAGEMENT_METHODS = frozenset(
    {
        ReqMethod.EXTENSIONS_V2_QUERY.value,
        ReqMethod.EXTENSIONS_V2_CATALOG.value,
        ReqMethod.EXTENSIONS_V2_SHOW.value,
        ReqMethod.EXTENSIONS_V2_IMPORT.value,
        ReqMethod.EXTENSIONS_V2_INSTALL.value,
        ReqMethod.EXTENSIONS_V2_UNINSTALL.value,
        ReqMethod.EXTENSIONS_V2_RELOAD.value,
    }
)
INVALID_PARAMS_CODE = "INVALID_PARAMS"


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
                timeout_seconds=120.0,
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
            manager=manager,
            resolve_client=resolve_client,
        )
        return True

    if manager is None:
        return False

    if method is ReqMethod.EXTENSIONS_V2_QUERY:
        package_id = params.get("package_id")
        if isinstance(package_id, str) and package_id:
            if not manager.owns(package_id):
                return False
            await _respond(channel, ws, req_id, ok=True, payload={"state": manager.query(package_id)})
            return True
        if not manager.gateway_installed():
            return False
        state = manager.query()
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
                timeout_seconds=120.0,
            )
            remote = payload.get("state") if ok else None
            if isinstance(remote, Mapping):
                state = merge_query_states(state, remote)
        await _respond(channel, ws, req_id, ok=True, payload={"state": state})
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
                timeout_seconds=120.0,
            )
            remote = payload.get("packages") if ok else None
            if isinstance(remote, list):
                packages = _merge_packages(packages, remote)
        await _respond(channel, ws, req_id, ok=True, payload={"packages": packages})
        return True

    if method is ReqMethod.EXTENSIONS_V2_SHOW:
        package_id = params.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            return False
        examples_dir = resolve_examples_dir()
        try:
            detail = package_detail(package_id, examples_dir, manager.query())
        except PackageNotFound:
            return False
        await _respond(channel, ws, req_id, ok=True, payload={"package": detail})
        return True

    if method is ReqMethod.EXTENSIONS_V2_INSTALL:
        examples_dir = resolve_examples_dir()
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
                paths = [
                    resolve_install_source(
                        package_id,
                        examples_dir=examples_dir,
                        install_dir=manager.install_dir,
                    )
                    for package_id in ids
                ]
            except PackageNotFound as exc:
                await _respond(channel, ws, req_id, ok=False, error=f"package {exc} not found", code="PACKAGE_NOT_FOUND")
                return True
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
        owners = [manager.owns_source(path) for path in paths]
        if not any(owners):
            return False
        if not all(owners):
            await _respond(
                channel,
                ws,
                req_id,
                ok=False,
                error="gateway and agent packages cannot be installed in one request",
                code=INVALID_PARAMS_CODE,
            )
            return True
        reports = await manager.install(paths)
        await _respond(
            channel,
            ws,
            req_id,
            ok=True,
            payload={
                "reports": [_encode(report) for report in reports],
                "installed_packages": manager.query()["installed_packages"],
            },
        )
        return True

    package_id = params.get("package_id")
    if not isinstance(package_id, str) or not manager.owns(package_id):
        return False
    operation = manager.uninstall if method is ReqMethod.EXTENSIONS_V2_UNINSTALL else manager.reload
    report = await operation(package_id)
    await _respond(channel, ws, req_id, ok=True, payload={"reports": [_encode(report)]})
    return True


async def _import_local(
    *,
    channel: Any,
    ws: Any,
    req_id: str,
    params: Mapping[str, Any],
    session_id: str | None,
    user_id: str | None,
    manager: Any | None,
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
        if manager is None:
            await _respond(
                channel, ws, req_id, ok=False, error="gateway extensions are disabled", code=INVALID_PARAMS_CODE
            )
            return
        reports = await manager.install([path])
        failed = next((report for report in reports if report.status != "SUCCESS"), None)
        if failed is not None:
            message = getattr(getattr(failed, "error", None), "message", None) or "package install failed"
            await _respond(channel, ws, req_id, ok=False, error=str(message), code="INSTALL_FAILED")
            return
        await _respond(
            channel,
            ws,
            req_id,
            ok=True,
            payload={"kind": "extension", "reports": [_encode(report) for report in reports]},
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
        timeout_seconds=120.0,
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
                entry = archive.getinfo("manifest.json")
                if entry.file_size > _MANIFEST_LIMIT:
                    return None
                raw = archive.read(entry)
        document = json.loads(raw)
    except (OSError, KeyError, BadZipFile, ReadError, TarError, UnicodeError, ValueError):
        return None
    return document if isinstance(document, Mapping) else None


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


def _merge_packages(local: list[dict[str, Any]], remote: list[Any]) -> list[dict[str, Any]]:
    """Merge Gateway cards into the AgentServer catalog, Gateway first."""

    merged = {str(item.get("id")): item for item in local if isinstance(item, Mapping)}
    for item in remote:
        if isinstance(item, Mapping):
            merged.setdefault(str(item.get("id")), dict(item))
    return list(merged.values())


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
