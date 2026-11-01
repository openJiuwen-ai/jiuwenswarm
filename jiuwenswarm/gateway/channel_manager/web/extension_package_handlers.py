# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Route V2 package-management requests to the Gateway or the AgentServer.

Gateway and AgentServer share ``extension_pkgs``; a request belongs to the
Gateway only when every addressed source declares ``domain: gateway``. Anything
else keeps the previous behaviour: the frame is proxied to the owning
AgentServer unchanged. Management frames never enter the extension bus, so an
extension cannot intercept its own uninstall.

The authentication gate applies when it is active: before a login-owning
package is published the Gateway has no authentication concept, so management
stays reachable and the first login package can be installed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary, proxy_unary_request

MANAGEMENT_METHODS = frozenset(
    {
        ReqMethod.EXTENSIONS_V2_QUERY.value,
        ReqMethod.EXTENSIONS_V2_INSTALL.value,
        ReqMethod.EXTENSIONS_V2_UNINSTALL.value,
        ReqMethod.EXTENSIONS_V2_RELOAD.value,
    }
)
AUTH_REQUIRED_CODE = "AUTH_REQUIRED"
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
        ReqMethod.EXTENSIONS_V2_INSTALL,
        ReqMethod.EXTENSIONS_V2_UNINSTALL,
        ReqMethod.EXTENSIONS_V2_RELOAD,
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

    if method.value not in MANAGEMENT_METHODS:
        return False
    runtime = get_gateway_runtime()
    if runtime is None or not runtime.config.enabled or not isinstance(params, Mapping):
        return False
    manager = runtime.packages

    if runtime.auth_required:
        connection_id = await runtime.connection_id(ws)
        if not runtime.is_authenticated(connection_id):
            await _respond(
                channel,
                ws,
                req_id,
                ok=False,
                error="login required to manage packages",
                code=AUTH_REQUIRED_CODE,
            )
            return True

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
                state = _merge_state(state, remote)
        await _respond(channel, ws, req_id, ok=True, payload={"state": state})
        return True

    if method is ReqMethod.EXTENSIONS_V2_INSTALL:
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


def _merge_state(local: Mapping[str, Any], remote: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the Gateway half into the AgentServer query state."""

    merged = dict(remote)
    merged["active"] = list(local.get("active") or []) + list(remote.get("active") or [])
    merged["installed"] = list(local.get("installed") or []) + list(remote.get("installed") or [])
    merged["installed_packages"] = (
        list(local.get("installed_packages") or []) + list(remote.get("installed_packages") or [])
    )
    merged["published"] = {**remote.get("published", {}), **local.get("published", {})}
    merged["inflight"] = {**remote.get("inflight", {}), **local.get("inflight", {})}
    return merged


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
