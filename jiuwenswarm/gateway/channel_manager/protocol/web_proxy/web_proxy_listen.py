# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Per-agent_type listen ports and the ``3rdagent.web`` control method."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    AgentRuntime,
    is_third_party_agent_type,
)
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
    _HTTP_METHODS,
    _TEST_NET_1,
    _WILDCARD_HOSTS,
    WebProxyChannelConfig,
    _append_tail,
    _authenticate_web_proxy,
    _auth_error_response,
    _client_remote,
    _cookie_max_age,
    _cookie_named,
    _ensure_http_proxy_session,
    _ensure_proxy_session,
    _extract_web_proxy_token,
    _filter_response_headers,
    _final_response_headers,
    _forward_headers,
    _forward_ws_headers,
    _full_request_path,
    _location_without_token,
    _merge_request_query,
    _query_access_token,
    _release_web_runtime,
    _retry_after_seconds,
    _sandbox_creating_response,
    _set_session_cookie,
    WEB_PROXY_USER_COOKIE,
    WebProxyAuthError,
)

logger = logging.getLogger(__name__)

# How often the background keeper retries ports that failed to bind.
_BIND_RETRY_INTERVAL = 5.0


@dataclass
class _PortBinding:
    agent_type: str
    port: int
    server: Any
    task: asyncio.Task[None]
    sock: socket.socket
    closed: bool = False

    async def begin(self) -> bool:
        return not self.closed


def _proxy_config(channel: Any) -> WebProxyChannelConfig:
    config = getattr(channel, "web_proxy_config", None)
    if isinstance(config, WebProxyChannelConfig):
        return config
    return WebProxyChannelConfig()


def _is_wildcard_host(host: str) -> bool:
    return (host or "").strip().lower() in _WILDCARD_HOSTS


def _outbound_ip() -> str:
    """Best-effort non-loopback IPv4 of the outbound interface.

    A UDP ``connect`` only makes the kernel pick a route (no packet is sent);
    ``getsockname()`` then yields the local address of that interface.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((_TEST_NET_1, 9))
        return str(sock.getsockname()[0] or "")
    except OSError:
        return ""
    finally:
        sock.close()


def resolve_advertise_host(channel: Any) -> str:
    """Host written into ``3rdagent.web`` ``url``.

    Prefer ``advertise_host``, then ``GATEWAY_HOST``, then a concrete listen
    address. A wildcard listen falls back to the outbound IP, and to
    ``127.0.0.1`` only when the process is bound to loopback.
    """
    config = _proxy_config(channel)
    advertised = str(config.advertise_host or "").strip()
    if advertised:
        return advertised
    env_host = str(os.environ.get("GATEWAY_HOST") or "").strip()
    if env_host:
        return env_host
    listen_host = str(config.listen_host or "").strip()
    channel_host = str(getattr(getattr(channel, "config", None), "host", "") or "").strip()
    for candidate in (listen_host, channel_host):
        if candidate and not _is_wildcard_host(candidate):
            return candidate
    if listen_host in {"127.0.0.1", "localhost"} or channel_host in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    outbound = _outbound_ip()
    if outbound and outbound != "127.0.0.1":
        return outbound
    return "127.0.0.1"


def build_web_url(*, host: str, port: int, user_id: str, token: str = "") -> str:
    query: dict[str, str] = {"user_id": user_id}
    if token:
        query["token"] = token
    return f"http://{host}:{port}/?{urllib.parse.urlencode(query)}"


def _bind_listen_socket(host: str, port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        return None
    return sock


class WebPortManager:
    """``agent_type →`` one northbound listen port, shared by every user.

    Ports are decided *outside* the Gateway (the registry writes them to etcd);
    :meth:`reconcile` makes the listeners match a desired ``{agent_type: port}``
    map. A port change closes the old listener and binds the new one. There is
    no idle reclamation: a listener stays until it leaves the desired map or the
    manager is closed.
    """

    def __init__(self, channel: Any, config: WebProxyChannelConfig | None = None) -> None:
        self._channel = channel
        self._config = config or _proxy_config(channel)
        self._bindings: dict[str, _PortBinding] = {}
        self._desired: dict[str, int] = {}
        self._failed: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._keeper: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def config(self) -> WebProxyChannelConfig:
        return self._config

    def update_config(self, config: WebProxyChannelConfig) -> None:
        self._config = config
        self._channel.web_proxy_config = config

    def start(self) -> None:
        if self._closed:
            self._closed = False
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.create_task(
                self._keeper_loop(), name="web-proxy-bind-keeper"
            )

    def port_for(self, agent_type: str) -> int | None:
        """The bound listen port for *agent_type*, or ``None`` if not bound."""
        binding = self._bindings.get(agent_type)
        if binding is None or binding.closed:
            return None
        return binding.port

    def desired_port(self, agent_type: str) -> int | None:
        """The registered port for *agent_type*, whether or not it is bound yet."""
        return self._desired.get(agent_type)

    @property
    def closed(self) -> bool:
        """True once :meth:`close_all` has run; no listener will be bound."""
        return self._closed

    async def reconcile(self, desired: dict[str, int]) -> None:
        """Make listeners match *desired* ``{agent_type: port}``.

        Closes listeners that were removed or whose port changed, then binds the
        missing/changed ones. Idempotent; safe to call on every registry change.
        """
        async with self._lock:
            self._desired = {str(k): int(v) for k, v in desired.items()}
            stale: list[_PortBinding] = []
            for agent_type, binding in list(self._bindings.items()):
                want = self._desired.get(agent_type)
                if binding.closed or want is None or want != binding.port:
                    binding.closed = True
                    self._bindings.pop(agent_type, None)
                    stale.append(binding)
            to_bind = [
                (agent_type, port)
                for agent_type, port in self._desired.items()
                if agent_type not in self._bindings
            ]
        for binding in stale:
            await _stop_binding(binding)
            logger.info(
                "[WebProxy] listener closed: agent=%s port=%s",
                binding.agent_type,
                binding.port,
            )
        for agent_type, port in to_bind:
            await self._bind(agent_type, port)
        logger.debug(
            "[WebProxy] reconcile desired=%s bound=%s",
            sorted(self._desired),
            sorted(self._bindings),
        )

    async def _bind(self, agent_type: str, port: int) -> None:
        """Bind *port* for *agent_type* unless it is taken or binding fails."""
        async with self._lock:
            if self._closed or agent_type in self._bindings:
                return
            if self._desired.get(agent_type) != port:
                return
            for other, binding in self._bindings.items():
                if binding.port == port and other != agent_type:
                    logger.error(
                        "[WebProxy] port %s already used by agent=%s; skip agent=%s",
                        port,
                        other,
                        agent_type,
                    )
                    return
            listen_host = str(self._config.listen_host or "0.0.0.0").strip() or "0.0.0.0"
            sock = _bind_listen_socket(listen_host, port)
            if sock is None:
                self._failed[agent_type] = port
                logger.warning(
                    "[WebProxy] bind failed: agent=%s %s:%s (will retry)",
                    agent_type,
                    listen_host,
                    port,
                )
                return
        try:
            binding = await _start_listener(
                self._channel, agent_type, port, sock, listen_host
            )
        except Exception:
            sock.close()
            self._failed[agent_type] = port
            logger.exception(
                "[WebProxy] listener start failed: agent=%s port=%s", agent_type, port
            )
            return
        async with self._lock:
            abandon = self._closed or self._desired.get(agent_type) != port
            if not abandon:
                self._bindings[agent_type] = binding
                self._failed.pop(agent_type, None)
        if abandon:
            await _stop_binding(binding)
            return
        logger.info("[WebProxy] listening: agent=%s %s:%s", agent_type, listen_host, port)

    async def _keeper_loop(self) -> None:
        """Retry ports that failed to bind, until the manager is closed."""
        while not self._closed:
            try:
                await asyncio.sleep(_BIND_RETRY_INTERVAL)
                if self._closed:
                    return
                for agent_type, port in list(self._failed.items()):
                    if self._desired.get(agent_type) == port:
                        await self._bind(agent_type, port)
            except Exception:
                logger.exception("[WebProxy] bind keeper failed")

    async def close_all(self) -> None:
        async with self._lock:
            self._closed = True
            self._desired = {}
            self._failed = {}
            bindings = list(self._bindings.values())
            self._bindings.clear()
            keeper = self._keeper
            self._keeper = None
        if keeper is not None:
            keeper.cancel()
            try:
                await keeper
            except asyncio.CancelledError:
                pass
        for binding in bindings:
            await _stop_binding(binding)
        for attr in ("web_proxy_session", "web_proxy_http_session"):
            session = getattr(self._channel, attr, None)
            if session is not None and not getattr(session, "closed", True):
                await session.close()
                setattr(self._channel, attr, None)


async def _start_listener(
    channel: Any,
    agent_type: str,
    port: int,
    sock: socket.socket,
    listen_host: str,
) -> _PortBinding:
    import uvicorn

    app = build_agent_port_app(channel, agent_type)
    config = uvicorn.Config(
        app,
        host=listen_host,
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(
        _serve_without_signals(server, sock), name=f"web-proxy-{port}"
    )
    binding = _PortBinding(
        agent_type=agent_type,
        port=port,
        server=server,
        task=task,
        sock=sock,
    )
    app.state.binding = binding
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return binding
        if task.done():
            exc = task.exception()
            raise RuntimeError(f"web proxy listener exited before ready: {exc}")
        await asyncio.sleep(0.02)
    server.should_exit = True
    raise RuntimeError(f"web proxy listener on :{port} did not start")


async def _serve_without_signals(server: Any, sock: socket.socket) -> None:
    """Same steps as ``uvicorn.Server._serve``, without capturing signals.

    ``uvicorn.Server.serve`` wraps ``_serve`` in ``capture_signals``, which
    installs SIGINT/SIGTERM handlers whenever the loop runs in the main thread
    — the Gateway's primary WebChannel server owns those. Assembling the same
    lifecycle from public methods keeps that ownership intact.
    """
    if not server.config.loaded:
        server.config.load()
    server.lifespan = server.config.lifespan_class(server.config)  # type: ignore[attr-defined]
    await server.startup(sockets=[sock])
    if not server.should_exit:
        await server.main_loop()
    if server.started:
        await server.shutdown(sockets=[sock])


async def _stop_binding(binding: _PortBinding) -> None:
    binding.closed = True
    server = binding.server
    task = binding.task
    if server is not None:
        server.should_exit = True
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(task, timeout=3)
    except asyncio.TimeoutError:
        if server is not None:
            server.force_exit = True
        try:
            await asyncio.wait_for(task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    except Exception:
        logger.debug("[WebProxy] listener stop ignored", exc_info=True)


def build_agent_port_app(channel: Any, agent_type: str) -> FastAPI:
    """ASGI app for one agent_type. Paths are the container Web's own paths."""
    app = FastAPI(
        title=f"JiuwenSwarm Web Proxy {agent_type}",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.agent_type = agent_type
    app.state.channel = channel
    app.state.binding = None

    async def http_proxy(request: Request, tail: str = "") -> Response:
        return await _handle_http_proxy(app, request, tail=tail)

    async def ws_proxy(websocket: WebSocket, tail: str = "") -> None:
        await _handle_ws_proxy(app, websocket, tail=tail)

    app.add_api_route("/", http_proxy, methods=_HTTP_METHODS, include_in_schema=False)
    app.add_api_route(
        "/{tail:path}",
        http_proxy,
        methods=_HTTP_METHODS,
        include_in_schema=False,
    )
    app.add_api_websocket_route("/", ws_proxy)
    app.add_api_websocket_route("/{tail:path}", ws_proxy)
    return app


async def _activity_begin(app: FastAPI) -> bool:
    """False once the listener is being torn down (the request then 404s)."""
    binding = getattr(app.state, "binding", None)
    if binding is None:
        return True
    return await binding.begin()


async def _handle_http_proxy(app: FastAPI, request: Request, *, tail: str) -> Response:
    channel = app.state.channel
    agent_type = str(app.state.agent_type or "")
    tail = (tail or "").strip("/")
    if not getattr(channel, "web_proxy_enabled", False):
        return PlainTextResponse("not found", status_code=404)
    if not await _activity_begin(app):
        return PlainTextResponse("not found", status_code=404)

    prepared = await _prepare_http(channel, request, agent_type=agent_type, tail=tail)
    if isinstance(prepared, Response):
        return prepared
    user_id, session_token, held_tail = prepared
    return await _proxy_http(
        app,
        channel,
        request,
        agent_type=agent_type,
        user_id=user_id,
        session_token=session_token,
        tail=held_tail,
    )


async def _prepare_http(
    channel: Any,
    request: Request,
    *,
    agent_type: str,
    tail: str,
) -> Response | tuple[str, str | None, str]:
    del agent_type
    referer = request.headers.get("referer") or ""
    query_user_id = (request.query_params.get("user_id") or "").strip()
    if not query_user_id:
        query_user_id = _cookie_named(request.headers, WEB_PROXY_USER_COOKIE)
    auth_path = _full_request_path(str(request.url.path or ""), str(request.url.query or ""))
    try:
        user_id, session_token = await _authenticate_web_proxy(
            channel,
            path=auth_path,
            headers=request.headers,
            remote=_client_remote(request.client),
            query_user_id=query_user_id,
            referer=referer,
        )
    except WebProxyAuthError as exc:
        return _auth_error_response(exc)
    if _query_access_token(str(request.url.query or "")):
        location = _location_without_token(request, user_id)
        return _set_session_cookie(
            RedirectResponse(url=location, status_code=302),
            session_token,
            user_id,
            max_age=_cookie_max_age(channel),
        )
    if not user_id:
        return PlainTextResponse("user_id query parameter is required", status_code=400)
    return user_id, session_token, tail


def _request_has_body(request: Request) -> bool:
    """True when the browser request carries a body to forward upstream."""
    if "transfer-encoding" in request.headers:
        return True
    length = str(request.headers.get("content-length") or "").strip()
    return length.isdigit() and int(length) > 0


async def _iter_request_body(request: Request):
    """Stream the request body upstream instead of buffering it whole."""
    async for chunk in request.stream():
        if chunk:
            yield chunk


async def _proxy_http(
    app: FastAPI,
    channel: Any,
    request: Request,
    *,
    agent_type: str,
    user_id: str,
    session_token: str | None,
    tail: str,
) -> Response:
    resolver = getattr(channel, "web_resolver", None)
    if not callable(resolver):
        return PlainTextResponse("web resolver is not configured", status_code=503)

    held = False
    defer_release = False
    try:
        try:
            endpoint = await resolver(user_id, agent_type, "http")
        except Exception as exc:
            if _retry_after_seconds(exc) is not None:
                return _sandbox_creating_response(exc)
            raise
        if not endpoint:
            return PlainTextResponse(
                f"agent not found or no http endpoint: user={user_id} agent_type={agent_type}",
                status_code=404,
            )
        held = True
        session = await _ensure_http_proxy_session(channel)
        upstream_url = _append_tail(endpoint, tail)
        upstream_url = _merge_request_query(
            upstream_url, str(request.url.query or "")
        )
        logger.info(
            "[WebProxy] HTTP proxy: agent=%s method=%s path=/%s",
            agent_type,
            request.method,
            tail,
        )
        try:
            ctx = session.request(
                request.method,
                upstream_url,
                headers=_forward_headers(
                    request.headers,
                    client_host=str(getattr(request.client, "host", "") or ""),
                    user_id=user_id,
                ),
                data=(
                    _iter_request_body(request)
                    if _request_has_body(request)
                    else None
                ),
            )
            upstream_resp = await ctx.__aenter__()
        except aiohttp.ClientError as exc:
            logger.warning("[WebProxy] HTTP proxy failed: agent=%s path=/%s", agent_type, tail)
            logger.debug("[WebProxy] HTTP proxy error detail: %s", exc)
            return PlainTextResponse(f"upstream error: {exc}", status_code=502)

        method = request.method.upper()
        status = upstream_resp.status
        content_type = upstream_resp.headers.get("Content-Type", "")
        no_body = method == "HEAD" or status in (204, 304) or 100 <= status < 200

        if not no_body and "text/event-stream" in content_type:
            upstream_headers = [
                (name, value)
                for name, value in _filter_response_headers(upstream_resp.raw_headers)
                if name != b"content-length"
            ]
            if not any(name == b"cache-control" for name, _ in upstream_headers):
                upstream_headers.append((b"cache-control", b"no-cache"))

            async def _stream():
                try:
                    async for chunk in upstream_resp.content.iter_any():
                        yield chunk
                finally:
                    await ctx.__aexit__(None, None, None)
                    await _release_web_runtime(channel, user_id, agent_type)

            defer_release = True
            response = StreamingResponse(_stream(), status_code=status)
            response.raw_headers = upstream_headers
            return _set_session_cookie(
                response, session_token, user_id, max_age=_cookie_max_age(channel)
            )

        try:
            resp_body = b"" if no_body else await upstream_resp.read()
            upstream_headers = _filter_response_headers(upstream_resp.raw_headers)
        finally:
            await ctx.__aexit__(None, None, None)
        response = Response(content=resp_body, status_code=status)
        # Upstream business headers are passed through (Set-Cookie included);
        # Content-Length is recomputed to match the bytes we actually send.
        response.raw_headers = _final_response_headers(
            upstream_headers, method=method, status=status, body=resp_body
        )
        return _set_session_cookie(
            response, session_token, user_id, max_age=_cookie_max_age(channel)
        )
    finally:
        if held and not defer_release:
            await _release_web_runtime(channel, user_id, agent_type)


async def _handle_ws_proxy(app: FastAPI, websocket: WebSocket, *, tail: str) -> None:
    channel = app.state.channel
    agent_type = str(app.state.agent_type or "")
    tail = (tail or "").strip("/")
    if not getattr(channel, "web_proxy_enabled", False):
        await websocket.close(code=1008, reason="not found")
        return
    if not await _activity_begin(app):
        await websocket.close(code=1008, reason="not found")
        return
    await _proxy_ws(channel, websocket, agent_type=agent_type, tail=tail)


async def _proxy_ws(
    channel: Any,
    websocket: WebSocket,
    *,
    agent_type: str,
    tail: str,
) -> None:
    referer = websocket.headers.get("referer") or ""
    query_user_id = (websocket.query_params.get("user_id") or "").strip()
    if not query_user_id:
        query_user_id = _cookie_named(websocket.headers, WEB_PROXY_USER_COOKIE)
    auth_path = _full_request_path(str(websocket.url.path or ""), str(websocket.url.query or ""))
    try:
        user_id, _session_token = await _authenticate_web_proxy(
            channel,
            path=auth_path,
            headers=websocket.headers,
            remote=_client_remote(websocket.client),
            query_user_id=query_user_id,
            referer=referer,
        )
    except WebProxyAuthError:
        await websocket.close(code=1008, reason="unauthorized")
        return
    if not user_id:
        await websocket.close(code=1008, reason="user_id is required")
        return
    resolver = getattr(channel, "web_resolver", None)
    if not callable(resolver):
        await websocket.close(code=1013, reason="web resolver is not configured")
        return

    held = False
    try:
        try:
            endpoint = await resolver(user_id, agent_type, "ws")
        except Exception as exc:
            if _retry_after_seconds(exc) is not None:
                await websocket.close(code=1013, reason="agent sandbox is creating")
                return
            raise
        if not endpoint:
            await websocket.close(code=1008, reason="agent not found")
            return
        held = True
        session = await _ensure_proxy_session(channel)
        upstream_url = _append_tail(endpoint, tail)
        ws_headers = _forward_ws_headers(
            websocket.headers,
            client_host=str(getattr(websocket.client, "host", "") or ""),
            user_id=user_id,
        )
        logger.info("[WebProxy] WS tunnel: agent=%s path=/%s", agent_type, tail)
        try:
            ws_ctx = session.ws_connect(upstream_url, headers=ws_headers)
            ws_upstream = await ws_ctx.__aenter__()
        except aiohttp.ClientError as exc:
            logger.warning("[WebProxy] WS proxy failed: agent=%s path=/%s", agent_type, tail)
            logger.debug("[WebProxy] WS proxy error detail: %s", exc)
            if websocket.client_state.name != "DISCONNECTED":
                try:
                    await websocket.close(code=1011, reason="upstream websocket failed")
                except Exception:
                    logger.debug("[WebProxy] close client websocket ignored", exc_info=True)
            return
        await websocket.accept()
        tasks = [
            asyncio.create_task(_pump_starlette_to_upstream(websocket, ws_upstream)),
            asyncio.create_task(_pump_upstream_to_starlette(ws_upstream, websocket)),
        ]
        try:
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            try:
                await ws_ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("[WebProxy] close upstream websocket ignored", exc_info=True)
            if websocket.client_state.name != "DISCONNECTED":
                try:
                    await websocket.close()
                except Exception:
                    logger.debug("[WebProxy] close client websocket ignored", exc_info=True)
    finally:
        if held:
            await _release_web_runtime(channel, user_id, agent_type)


async def _pump_starlette_to_upstream(
    src: WebSocket,
    dst: aiohttp.ClientWebSocketResponse,
) -> None:
    try:
        while True:
            msg = await src.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            data = msg.get("bytes")
            if text is not None:
                await dst.send_str(text)
            elif data is not None:
                await dst.send_bytes(data)
    except WebSocketDisconnect:
        return


async def _pump_upstream_to_starlette(
    src: aiohttp.ClientWebSocketResponse,
    dst: WebSocket,
) -> None:
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_text(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            break


def _session_token(ws: Any) -> str:
    path = str(getattr(ws, "path", "") or "")
    headers = getattr(ws, "request_headers", None) or {}
    return _extract_web_proxy_token(path, headers, "")


def register_3rdagent_web_method(channel: Any) -> None:
    """Register ``3rdagent.web`` on the WebChannel method table."""

    async def _handler(
        ws: Any,
        req_id: str,
        params: dict[str, Any],
        session_id: str,
        user_id: str | None = None,
    ) -> None:
        del session_id
        await handle_3rdagent_web(channel, ws, req_id, params, user_id=user_id or "")

    channel.register_method("3rdagent.web", _handler)


async def handle_3rdagent_web(
    channel: Any,
    ws: Any,
    req_id: str,
    params: dict[str, Any] | None,
    *,
    user_id: str = "",
) -> None:
    """Report the northbound port for ``params.agent_type``.

    The port is decided by the registry and applied by ``registry_watcher``;
    this method never allocates or binds. It reads the live listener table and,
    as a side effect, warms up the current user's sandbox (so the first browser
    open is not a 503). The ``url`` is only returned once the sandbox resolves.
    """
    body = params if isinstance(params, dict) else {}
    raw_type = str(body.get("agent_type") or "").strip()
    if not raw_type:
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="agent_type is required",
            code="BAD_REQUEST",
        )
        return
    if not getattr(channel, "web_proxy_enabled", False):
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="web proxy is disabled",
            code="WEB_PROXY_DISABLED",
        )
        return

    uid = str(user_id or "").strip()
    if not uid:
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="user_id is required",
            code="BAD_REQUEST",
        )
        return

    agent_type = AgentRuntime.normalize_agent_type(raw_type)
    if not is_third_party_agent_type(agent_type):
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="agent has no http web endpoint",
            code="NO_WEB_ENDPOINT",
        )
        return

    auth_on = bool(getattr(channel, "web_proxy_auth_enabled", True))
    token = _session_token(ws) if auth_on else ""
    if auth_on and not token:
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="token is required on the web session",
            code="BAD_REQUEST",
        )
        return

    manager = _manager_for(channel)
    desired_port = manager.desired_port(agent_type)
    if desired_port is None:
        logger.debug("[WebProxy] no web endpoint registered for agent=%s", agent_type)
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="agent has no http web endpoint",
            code="NO_WEB_ENDPOINT",
        )
        return
    bound_port = manager.port_for(agent_type)
    if bound_port is None:
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="agent web port is not listening yet",
            code="PORT_NOT_LISTENING",
            payload={"agent_type": agent_type, "port": desired_port},
        )
        return

    resolver = getattr(channel, "web_resolver", None)
    held = False
    try:
        if callable(resolver):
            try:
                endpoint = await resolver(uid, agent_type, "http")
            except Exception as exc:
                seconds = _retry_after_seconds(exc)
                if seconds is not None:
                    await channel.send_response(
                        ws,
                        req_id,
                        ok=False,
                        error="agent sandbox is creating",
                        code="AGENT_CREATING",
                        payload={
                            "agent_type": agent_type,
                            "port": bound_port,
                            "retry_after_sec": seconds,
                        },
                    )
                    return
                logger.exception(
                    "[WebProxy] 3rdagent.web resolve failed: agent=%s", agent_type
                )
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="web endpoint resolve failed",
                    code="NO_WEB_ENDPOINT",
                )
                return
            if not endpoint:
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="agent has no http web endpoint",
                    code="NO_WEB_ENDPOINT",
                )
                return
            held = True

        host = resolve_advertise_host(channel)
        url = build_web_url(host=host, port=bound_port, user_id=uid, token=token)
        logger.info("[WebProxy] 3rdagent.web: agent=%s port=%s", agent_type, bound_port)
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "agent_type": agent_type,
                "host": host,
                "port": bound_port,
                "url": url,
            },
        )
    finally:
        if held:
            await _release_web_runtime(channel, uid, agent_type)


def _manager_for(channel: Any) -> WebPortManager:
    manager = getattr(channel, "web_port_manager", None)
    if isinstance(manager, WebPortManager) and not manager.closed:
        return manager
    created = WebPortManager(channel, _proxy_config(channel))
    channel.web_port_manager = created
    created.start()
    return created
