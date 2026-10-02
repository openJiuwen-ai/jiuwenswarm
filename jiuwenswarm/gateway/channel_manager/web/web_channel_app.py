# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FastAPI app for WebChannel: WebSocket and HTTP on the same port."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse

from jiuwenswarm.common.security.ws_origin import (
    get_header_value,
    is_allowed_browser_origin,
    is_origin_check_enabled,
)
from jiuwenswarm.gateway.channel_manager.web.ws_connection_adapter import StarletteWsAdapter
from jiuwenswarm.extensions.application_host import (
    iter_websocket_routes,
    mount_application_plugin_http_routes,
)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

_FORBIDDEN_ORIGIN_BODY = "Forbidden: Origin not allowed\n"
_UNAUTHORIZED_BODY = "Unauthorized\n"
_GIT_WS_PATH = "/ws/git"


def normalize_web_ws_path(path: str | None) -> str:
    """Normalize WebChannelConfig.path for FastAPI websocket registration."""
    text = str(path or "").strip() or "/ws"
    if not text.startswith("/"):
        text = f"/{text}"
    # Strip trailing slashes except root; keep "/ws" style paths stable.
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def build_web_channel_app(channel: WebChannel) -> FastAPI:
    """Build the same-port HTTP+WS app bound to one WebChannel instance.

    WebSocket endpoints for ``channel.config.path`` (default ``/ws``) and
    hard-coded ``/ws/git``. HTTP routes (e.g. ``/file-api``) register on the
    same ``app`` without changing existing WS JSON-RPC methods.
    """
    app = FastAPI(
        title="JiuwenSwarm WebChannel",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web_channel = channel
    plugin_registry = getattr(channel, "application_plugin_registry", None)

    register_http_routes(app, channel)
    mount_application_plugin_http_routes(app, plugin_registry)

    main_path = normalize_web_ws_path(channel.config.path)

    async def websocket_endpoint(websocket: WebSocket) -> None:
        await _serve_channel_websocket(channel, websocket)

    # Honor WEB_PATH / --web-path (same as handle_connection path check).
    app.add_api_websocket_route(main_path, websocket_endpoint)
    reserved_paths = {main_path, _GIT_WS_PATH}
    for plugin_id, route in iter_websocket_routes(plugin_registry):
        path = normalize_web_ws_path(route.path)
        if path in reserved_paths:
            raise ValueError(
                f"application plugin {plugin_id} websocket route conflicts with {path}"
            )
        reserved_paths.add(path)

        def _plugin_endpoint_factory(plugin_identifier, route_contribution):
            async def _plugin_endpoint(websocket: WebSocket) -> None:
                if route_contribution.check_origin and await _reject_disallowed_origin(websocket):
                    return
                plugin = (
                    plugin_registry.get_application_plugin(plugin_identifier)
                    if plugin_registry is not None
                    else None
                )
                if plugin is None or (
                    not route_contribution.available_when_disabled
                    and not plugin.is_enabled()
                ):
                    await websocket.close(code=1008, reason="application plugin is disabled")
                    return
                await route_contribution.endpoint(websocket)

            return _plugin_endpoint

        app.add_api_websocket_route(path, _plugin_endpoint_factory(plugin_id, route))
    if main_path != _GIT_WS_PATH:
        app.add_api_websocket_route(_GIT_WS_PATH, websocket_endpoint)
    else:
        logger.warning(
            "WebChannel config.path=%s conflicts with git WS path; "
            "only registering once (git handler wins in handle_connection)",
            main_path,
        )

    return app


def register_http_routes(app: FastAPI, channel: WebChannel) -> None:
    """Register optional HTTP routes on the WebChannel port.

    Container file APIs are mounted when ``channel.container_file_client`` is an
    ``AgentOSRouterClient`` (wired during web handler registration).
    """
    from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
        attach_container_file_routes,
    )
    from jiuwenswarm.gateway.channel_manager.web.trajectory_http import (
        attach_trajectory_routes,
    )
    from jiuwenswarm.gateway.channel_manager.web.web_http_auth import register_auth_routes

    attach_container_file_routes(app, channel)
    attach_trajectory_routes(app, channel)
    # register auth http routes for huawei account login
    register_auth_routes(app)


async def _serve_channel_websocket(channel: WebChannel, websocket: WebSocket) -> None:
    if await _reject_disallowed_origin(websocket):
        return
    denied, authenticated_user_id = await _reject_unauthorized_handshake(channel, websocket)
    if denied:
        return

    adapter = StarletteWsAdapter(websocket, authenticated_user_id=authenticated_user_id)
    await adapter.accept()
    try:
        await channel.handle_connection(adapter, path=adapter.path)
    except Exception:  # noqa: BLE001 — connection-level isolation
        logger.exception(
            "WebChannel WS handler crashed path=%s remote=%s",
            adapter.path,
            adapter.remote_address,
        )
        if not adapter.closed:
            await adapter.close(code=1011, reason="internal error")


async def _reject_disallowed_origin(websocket: WebSocket) -> bool:
    """Return True when the handshake was rejected (HTTP 403)."""
    enable_origin_check = is_origin_check_enabled()
    origin = get_header_value(websocket.headers, "Origin")
    if not enable_origin_check:
        logger.info(
            "WebChannel 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
            websocket.url.path,
            origin,
            enable_origin_check,
            True,
        )
        return False

    allowed = is_allowed_browser_origin(origin)
    logger.info(
        "WebChannel 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
        websocket.url.path,
        origin,
        enable_origin_check,
        allowed,
    )
    if allowed:
        return False

    logger.warning(
        "WebChannel 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
        websocket.url.path,
        origin,
    )
    await websocket.send_denial_response(
        PlainTextResponse(_FORBIDDEN_ORIGIN_BODY, status_code=403),
    )
    return True


def _websocket_handshake_path(websocket: WebSocket) -> str:
    path = str(websocket.url.path or "")
    query = str(websocket.url.query or "")
    return f"{path}?{query}" if query else path


async def _reject_unauthorized_handshake(
    channel: WebChannel, websocket: WebSocket
) -> tuple[bool, str | None]:
    """Reject unauthorized Upgrades and retain the authenticated user identity."""
    remote = ""
    client = getattr(websocket, "client", None)
    if client is not None:
        host = getattr(client, "host", "") or ""
        port = getattr(client, "port", "")
        remote = f"{host}:{port}" if host else ""
    result = await channel.authenticate_handshake(
        path=_websocket_handshake_path(websocket),
        headers=websocket.headers,
        remote=remote,
        channel="web",
    )
    if result is None:
        return False, None
    if bool(getattr(result, "success", True)):
        return False, str(getattr(result, "user_id", "") or "").strip()
    logger.warning(
        "WebChannel 握手拒绝 path=%s reason=unauthorized",
        websocket.url.path,
    )
    await websocket.send_denial_response(
        PlainTextResponse(_UNAUTHORIZED_BODY, status_code=401),
    )
    return True, None


def mount_future_http_router(app: FastAPI, router: Any) -> None:
    """Helper for later phases: ``app.include_router(router, prefix=...)``."""
    app.include_router(router)
