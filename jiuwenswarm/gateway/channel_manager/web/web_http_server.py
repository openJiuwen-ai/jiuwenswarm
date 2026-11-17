# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Start Gateway Web HTTP server (uvicorn) owned by WebChannel."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _parse_env_port(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not a valid port; using %s", name, raw, fallback)
        return fallback


def resolve_web_http_port(
    ws_port: int = 19000,
    *,
    http_port: int | None = None,
) -> int:
    """Resolve Gateway Web HTTP listen port.

    ``GATEWAY_WEB_HTTP_PORT`` overrides the computed port. When the chosen port
    collides with ``ws_port`` or ``GATEWAY_PORT``, returns the next free port.
    """
    ws_port = int(ws_port)
    port = int(http_port if http_port is not None else ws_port + 2)
    port = _parse_env_port("GATEWAY_WEB_HTTP_PORT", port)
    gateway_port = _parse_env_port("GATEWAY_PORT", ws_port + 1)
    if port in (ws_port, gateway_port):
        port = max(ws_port, gateway_port) + 1
    return port


def _env_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if allow_zero:
        return value if value >= 0 else default
    return value if value > 0 else default


def resolve_web_http_sse_timeout() -> float:
    """Total SSE stream lifetime seconds (default 0 = no cap).

    Long agent turns can run for hours; a hard cap would drop a live
    ``chat.send`` stream. Set ``GATEWAY_WEB_HTTP_SSE_TIMEOUT`` to a
    positive value only when ops wants an explicit lifetime limit.
    """
    return _env_float("GATEWAY_WEB_HTTP_SSE_TIMEOUT", 0.0, allow_zero=True)


def resolve_web_http_sse_idle_timeout() -> float:
    """Idle seconds without frames before ending SSE (default 0 = disabled)."""
    return _env_float("GATEWAY_WEB_HTTP_SSE_IDLE_TIMEOUT", 0.0, allow_zero=True)


def resolve_web_http_sse_keepalive() -> float:
    """Seconds between SSE keepalive comments while waiting (default 30)."""
    return _env_float("GATEWAY_WEB_HTTP_SSE_KEEPALIVE", 30.0)


def resolve_web_http_unary_timeout() -> float:
    """Unary ``wait_response`` timeout seconds (default 120)."""
    return _env_float("GATEWAY_WEB_HTTP_UNARY_TIMEOUT", 120.0)


def resolve_web_http_history_timeout() -> float:
    """History JSON collector timeout seconds (default 60)."""
    return _env_float("GATEWAY_WEB_HTTP_HISTORY_TIMEOUT", 60.0)


async def _wait_http_listen(
    host: str,
    port: int,
    task: asyncio.Task[Any],
    *,
    timeout: float = 5.0,
) -> None:
    """Wait until the HTTP port accepts TCP connections or the serve task fails."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    connect_host = host if host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    while loop.time() < deadline:
        if task.done():
            exc = task.exception()
            if exc:
                raise exc
            raise RuntimeError("web http server exited during startup")
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(connect_host, port),
                timeout=0.25,
            )
        except (OSError, asyncio.TimeoutError):
            # ConnectionRefusedError is an OSError subclass; do not list both.
            await asyncio.sleep(0.05)
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return
    raise TimeoutError(
        f"web http not accepting connections on {host}:{port} within {timeout}s",
    )


def _load_web_peer_fingerprints(peer_directory: Path) -> set[str]:
    from openjiuwen_runtime.foundation.security.link_profile import (
        LinkProfileError,
        cert_fingerprint,
    )

    try:
        certificates = sorted(
            path for path in peer_directory.iterdir() if path.suffix == ".crt"
        )
    except OSError as exc:
        raise LinkProfileError(
            f"cannot read Web mTLS peer directory {peer_directory}: {exc.strerror}"
        ) from exc

    fingerprints = set()
    for certificate in certificates:
        try:
            fingerprints.add(cert_fingerprint(str(certificate)))
        except (OSError, ValueError) as exc:
            logger.warning(
                "[WebHTTP] skipping invalid Web mTLS peer certificate %s (%s)",
                certificate,
                type(exc).__name__,
            )
    if not fingerprints:
        raise LinkProfileError(
            f"no valid Web mTLS peer certificates (*.crt) in {peer_directory}"
        )
    logger.info(
        "[WebHTTP] loaded %d Web mTLS peer fingerprints from %s",
        len(fingerprints),
        peer_directory,
    )
    return fingerprints


async def start_web_http_server(
    channel: Any,
    *,
    host: str,
    port: int,
) -> tuple[Any, asyncio.Task[Any]]:
    """Bind FastAPI Web HTTP app with uvicorn; return ``(server, serve_task)``."""
    import uvicorn

    from jiuwenswarm.gateway.channel_manager.web.web_http_app import create_web_http_app

    if port == getattr(getattr(channel, "config", None), "ws_port", None):
        logger.warning(
            "[WebHTTP] HTTP port %s equals WebChannel ws_port — "
            "WS and HTTP cannot share the same bind without multiplexing; "
            "start may fail. Use a distinct http_port (default 19002).",
            port,
        )

    app = create_web_http_app(channel)
    mode = os.getenv("JIUWENSWARM_LINK_MTLS_MODE", "").strip() or "off"
    tls_options: dict[str, Any] = {}
    if mode != "off":
        from jiuwenswarm.common.security.link_mtls import LinkMTLSConfig

        link_mtls = LinkMTLSConfig.from_env(role="gateway")
        if link_mtls.enforced:
            from openjiuwen_runtime.foundation.security.link_profile import (
                DEFAULT_IDENTITY_ROOT,
                PEER_CERT_SCOPE,
                LinkProfileError,
                fingerprint,
            )
            from openjiuwen_runtime.foundation.security.link_stream_guard import (
                LinkBindingMiddleware,
            )

            profile = link_mtls.profile
            if profile is None:
                raise LinkProfileError("enforce requires a pinned deployment profile")
            tls_options = link_mtls.uvicorn_ssl_kwargs()
            web_pins = _load_web_peer_fingerprints(DEFAULT_IDENTITY_ROOT / "web-peers")
            binding = {name.lower(): value for name, value in profile.headers().items()}

            def authorize_web(request: Any) -> None:
                peer = request.scope.get(PEER_CERT_SCOPE)
                health = request.scope.get("path") == "/api/v1/health"
                if health and (not peer or fingerprint(peer) not in web_pins):
                    # Preserve the existing service-identity checks for probes.
                    profile.authorize(request.scope, request.headers)
                    return
                profile.verify_peer(peer, {"web"}, expected_fingerprints=web_pins)
                if not health:
                    headers = {name.lower(): value for name, value in request.headers.items()}
                    if any(headers.get(name) != value for name, value in binding.items()):
                        raise LinkProfileError(
                            "request binding does not match authenticated deployment"
                        )

            app.add_middleware(LinkBindingMiddleware, authorize=authorize_web)
    cfg = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        lifespan="on",
        **tls_options,
    )
    if tls_options:
        # Surface TLS context and server credential errors before spawning the task.
        cfg.load()
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve(), name="gateway-web-http")
    await _wait_http_listen(host, port, task)
    logger.info(
        "[WebHTTP] listening %s://%s:%s/api/v1 "
        "(also /file-api /share-api /api/sessions*; health=/api/v1/health; mTLS mode=%s)",
        "https" if tls_options else "http",
        host,
        port,
        mode,
    )
    return server, task


async def stop_web_http_server(server: Any, task: asyncio.Task[Any] | None) -> None:
    if server is not None:
        server.should_exit = True
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[WebHTTP] serve task stop error", exc_info=True)
