# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Per-agent_type listen ports and the ``3rdagent.web`` control method."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    AgentRuntime,
    is_third_party_agent_type,
)
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
    DEFAULT_IDLE_TIMEOUT_SEC,
    DEFAULT_PORT_BASE,
    DEFAULT_PORT_SPAN,
    _HTTP_METHODS,
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
    _forward_headers,
    _forward_ws_headers,
    _full_request_path,
    _location_without_token,
    _query_access_token,
    _release_web_runtime,
    _retry_after_seconds,
    _sandbox_creating_response,
    _set_session_cookie,
    WEB_PROXY_USER_COOKIE,
    WebProxyAuthError,
)

logger = logging.getLogger(__name__)


class PortPoolExhausted(RuntimeError):
    """No free port left in ``[port_base, port_base + port_span)``."""


@dataclass
class _PortBinding:
    agent_type: str
    port: int
    server: Any
    task: asyncio.Task[None]
    sock: socket.socket
    last_active: float = field(default_factory=time.monotonic)
    inflight: int = 0
    closed: bool = False

    async def begin(self) -> bool:
        if self.closed:
            return False
        self.inflight += 1
        self.last_active = time.monotonic()
        return True

    def end(self) -> None:
        if self.inflight > 0:
            self.inflight -= 1
        self.last_active = time.monotonic()


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _proxy_config(channel: Any) -> WebProxyChannelConfig:
    config = getattr(channel, "web_proxy_config", None)
    if isinstance(config, WebProxyChannelConfig):
        return config
    return WebProxyChannelConfig()


def _is_wildcard_host(host: str) -> bool:
    return (host or "").strip().lower() in _WILDCARD_HOSTS


def _outbound_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
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
    """``agent_type →`` one northbound listen port, shared by every user."""

    def __init__(self, channel: Any, config: WebProxyChannelConfig | None = None) -> None:
        self._channel = channel
        self._config = config or _proxy_config(channel)
        self._bindings: dict[str, _PortBinding] = {}
        self._reserved_ports: set[int] = set()
        self._opening: dict[str, asyncio.Future[int]] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
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
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="web-proxy-port-reaper")

    def port_for(self, agent_type: str) -> int | None:
        binding = self._bindings.get(agent_type)
        if binding is None or binding.closed:
            return None
        return binding.port

    async def ensure(self, agent_type: str) -> int:
        """Return the listen port for *agent_type*, starting it on first use."""
        self.start()
        async with self._lock:
            if self._closed:
                raise PortPoolExhausted("web proxy port manager is closed")
            current = self._bindings.get(agent_type)
            if current is not None and not current.closed:
                current.last_active = time.monotonic()
                return current.port
            pending = self._opening.get(agent_type)
            if pending is not None:
                waiter = pending
                owner = False
                claimed: tuple[int, socket.socket, str] | None = None
            else:
                waiter = asyncio.get_running_loop().create_future()
                self._opening[agent_type] = waiter
                owner = True
                try:
                    claimed = self._claim_socket_locked()
                except PortPoolExhausted as exc:
                    self._opening.pop(agent_type, None)
                    if not waiter.done():
                        waiter.set_exception(exc)
                        waiter.exception()
                    raise
        if not owner:
            return await waiter
        assert claimed is not None
        port, sock, listen_host = claimed
        try:
            binding = await _start_listener(self._channel, agent_type, port, sock, listen_host)
        except Exception as exc:
            sock.close()
            async with self._lock:
                self._reserved_ports.discard(port)
                self._opening.pop(agent_type, None)
            if not waiter.done():
                waiter.set_exception(exc)
                waiter.exception()
            raise
        async with self._lock:
            self._reserved_ports.discard(port)
            self._opening.pop(agent_type, None)
            if self._closed:
                abandoned = True
            else:
                self._bindings[agent_type] = binding
                abandoned = False
        if abandoned:
            if not waiter.done():
                waiter.set_exception(PortPoolExhausted("web proxy port manager is closed"))
                waiter.exception()
            await _stop_binding(binding)
            raise PortPoolExhausted("web proxy port manager is closed")
        if not waiter.done():
            waiter.set_result(port)
        logger.info("[WebProxy] listening: agent=%s %s:%s", agent_type, listen_host, port)
        return port

    async def close_all(self) -> None:
        async with self._lock:
            self._closed = True
            bindings = list(self._bindings.values())
            self._bindings.clear()
            reaper = self._reaper
            self._reaper = None
        if reaper is not None:
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                pass
        for binding in bindings:
            await _stop_binding(binding)
        for attr in ("web_proxy_session", "web_proxy_http_session"):
            session = getattr(self._channel, attr, None)
            if session is not None and not getattr(session, "closed", True):
                await session.close()
                setattr(self._channel, attr, None)

    async def reap_idle(self, now: float | None = None) -> list[str]:
        """Close listeners with no in-flight traffic past ``idle_timeout_sec``."""
        moment = time.monotonic() if now is None else now
        timeout = _positive_int(self._config.idle_timeout_sec, DEFAULT_IDLE_TIMEOUT_SEC)
        stale: list[_PortBinding] = []
        async with self._lock:
            for agent_type, binding in list(self._bindings.items()):
                if binding.closed or binding.inflight > 0:
                    continue
                if moment - binding.last_active < timeout:
                    continue
                binding.closed = True
                self._bindings.pop(agent_type, None)
                stale.append(binding)
        stopped: list[str] = []
        for binding in stale:
            await _stop_binding(binding)
            stopped.append(binding.agent_type)
            logger.info(
                "[WebProxy] idle listener closed: agent=%s port=%s",
                binding.agent_type,
                binding.port,
            )
        return stopped

    async def _reap_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(min(30.0, max(1.0, self._config.idle_timeout_sec / 4)))
                if self._closed:
                    return
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[WebProxy] port reaper failed")

    def _claim_socket_locked(self) -> tuple[int, socket.socket, str]:
        """Pick and bind the next free port. Caller holds ``self._lock``."""
        base = _positive_int(self._config.port_base, DEFAULT_PORT_BASE)
        span = _positive_int(self._config.port_span, DEFAULT_PORT_SPAN)
        used = {item.port for item in self._bindings.values() if not item.closed}
        used.update(self._reserved_ports)
        listen_host = str(self._config.listen_host or "0.0.0.0").strip() or "0.0.0.0"
        for offset in range(span):
            port = base + offset
            if port in used:
                continue
            sock = _bind_listen_socket(listen_host, port)
            if sock is None:
                continue
            self._reserved_ports.add(port)
            return port, sock, listen_host
        raise PortPoolExhausted(f"no free web proxy port in [{base}, {base + span})")


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
    # Skip Server.serve(): it installs SIGINT/SIGTERM handlers and would
    # steal them from the Gateway's primary WebChannel server.
    task = asyncio.create_task(server._serve(sockets=[sock]), name=f"web-proxy-{port}")
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
    except asyncio.CancelledError:
        raise
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
    binding = getattr(app.state, "binding", None)
    if binding is None:
        return True
    manager = getattr(app.state.channel, "web_port_manager", None)
    lock = getattr(manager, "_lock", None)
    if lock is None:
        return await binding.begin()
    async with lock:
        return await binding.begin()


def _activity_end(app: FastAPI) -> None:
    binding = getattr(app.state, "binding", None)
    if binding is not None:
        binding.end()


async def _handle_http_proxy(app: FastAPI, request: Request, *, tail: str) -> Response:
    channel = app.state.channel
    agent_type = str(app.state.agent_type or "")
    tail = (tail or "").strip("/")
    if not getattr(channel, "web_proxy_enabled", False):
        return PlainTextResponse("not found", status_code=404)
    if not await _activity_begin(app):
        return PlainTextResponse("not found", status_code=404)

    defer_end = False
    try:
        prepared = await _prepare_http(channel, request, agent_type=agent_type, tail=tail)
        if isinstance(prepared, Response):
            return prepared
        user_id, session_token, held_tail = prepared
        response = await _proxy_http(
            app,
            channel,
            request,
            agent_type=agent_type,
            user_id=user_id,
            session_token=session_token,
            tail=held_tail,
        )
        if isinstance(response, StreamingResponse):
            defer_end = True
        return response
    finally:
        if not defer_end:
            _activity_end(app)


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
        body = await request.body()
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
                data=body if body else None,
            )
            upstream_resp = await ctx.__aenter__()
        except aiohttp.ClientError as exc:
            logger.warning("[WebProxy] HTTP proxy failed: agent=%s path=/%s", agent_type, tail)
            logger.debug("[WebProxy] HTTP proxy error detail: %s", exc)
            return PlainTextResponse(f"upstream error: {exc}", status_code=502)

        content_type = upstream_resp.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:

            async def _stream():
                try:
                    async for chunk in upstream_resp.content.iter_any():
                        yield chunk
                finally:
                    await ctx.__aexit__(None, None, None)
                    await _release_web_runtime(channel, user_id, agent_type)
                    _activity_end(app)

            defer_release = True
            return _set_session_cookie(
                StreamingResponse(
                    _stream(),
                    status_code=upstream_resp.status,
                    media_type=content_type,
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                ),
                session_token,
                user_id,
                max_age=_cookie_max_age(channel),
            )

        try:
            resp_body = await upstream_resp.read()
            media = upstream_resp.content_type
            status = upstream_resp.status
        finally:
            await ctx.__aexit__(None, None, None)
        return _set_session_cookie(
            Response(content=resp_body, status_code=status, media_type=media),
            session_token,
            user_id,
            max_age=_cookie_max_age(channel),
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
    try:
        await _proxy_ws(channel, websocket, agent_type=agent_type, tail=tail)
    finally:
        _activity_end(app)


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
    """Open (or reuse) the northbound port for ``params.agent_type``."""
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
    if not getattr(channel, "web_proxy_enabled", False) or not callable(
        getattr(channel, "web_resolver", None)
    ):
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

    resolver = channel.web_resolver
    held = False
    try:
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
                    payload={"retry_after_sec": seconds},
                )
                return
            logger.exception("[WebProxy] 3rdagent.web resolve failed: agent=%s", agent_type)
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
        manager = _manager_for(channel)
        try:
            port = await manager.ensure(agent_type)
        except PortPoolExhausted:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="web proxy port pool is exhausted",
                code="PORT_POOL_EXHAUSTED",
            )
            return
        host = resolve_advertise_host(channel)
        url = build_web_url(host=host, port=port, user_id=uid, token=token)
        logger.info("[WebProxy] 3rdagent.web: agent=%s port=%s", agent_type, port)
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "agent_type": agent_type,
                "host": host,
                "port": port,
                "url": url,
            },
        )
    finally:
        if held:
            await _release_web_runtime(channel, uid, agent_type)


def _manager_for(channel: Any) -> WebPortManager:
    manager = getattr(channel, "web_port_manager", None)
    if isinstance(manager, WebPortManager) and not manager._closed:
        return manager
    created = WebPortManager(channel, _proxy_config(channel))
    channel.web_port_manager = created
    created.start()
    return created
