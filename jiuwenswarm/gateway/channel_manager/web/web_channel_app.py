# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FastAPI app for WebChannel: WebSocket now, optional HTTP routes later (same port)."""

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

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

_FORBIDDEN_ORIGIN_BODY = "Forbidden: Origin not allowed\n"
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
    """Build the dual-protocol capable app bound to one WebChannel instance.

    Phase 1: WebSocket endpoints for ``channel.config.path`` (default ``/ws``)
    and hard-coded ``/ws/git``. Later: register HTTP routes on the same ``app``
    without changing existing WS JSON-RPC methods.
    """
    app = FastAPI(
        title="JiuwenSwarm WebChannel",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web_channel = channel

    # Extension point for future same-port HTTP (e.g. container file API).
    # Keep unused in Phase 1 so WS behavior stays the sole surface.
    register_http_routes(app, channel)

    main_path = normalize_web_ws_path(channel.config.path)

    async def websocket_endpoint(websocket: WebSocket) -> None:
        await _serve_channel_websocket(channel, websocket)

    # Honor WEB_PATH / --web-path (same as legacy handle_connection path check).
    app.add_api_websocket_route(main_path, websocket_endpoint)
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

    attach_container_file_routes(app, channel)


async def _serve_channel_websocket(channel: WebChannel, websocket: WebSocket) -> None:
    if await _reject_disallowed_origin(websocket):
        return

    adapter = StarletteWsAdapter(websocket)
    await adapter.accept()
    try:
        await channel.handle_connection(adapter, path=adapter.path)
    except Exception:  # noqa: BLE001 — connection-level isolation
        logger.exception(
            "WebChannel dual-protocol WS handler crashed path=%s remote=%s",
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


def mount_future_http_router(app: FastAPI, router: Any) -> None:
    """Helper for later phases: ``app.include_router(router, prefix=...)``."""
    app.include_router(router)
