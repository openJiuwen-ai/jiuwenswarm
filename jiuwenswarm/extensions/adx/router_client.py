# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-user E2A connections through ADX's generic managed WS entrypoint."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from websockets.exceptions import InvalidHandshake

from jiuwenswarm.common.client.agent_client import AgentServerClient
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.extensions.adx.config import AdxRouterConfig
from jiuwenswarm.extensions.adx.control_client import AdxControlClient
from jiuwenswarm.extensions.adx.errors import AdxRoutingError
from jiuwenswarm.extensions.adx.file_client import AdxFileMixin
from gateway.extensions.auth.credential_authenticator import AuthResult
from gateway.channel_manager.base import ChannelType
from gateway.health_check import HEALTH_CHECK_CHANNEL_ID, HEALTH_CHECK_OK
from gateway.routing.agent_client import (
    AgentServerUnaryTimeout,
    DuplicateRequestIdError,
    WebSocketAgentServerClient,
)

logger = logging.getLogger(__name__)
_DISCONNECT_CLEANUP_SECONDS = 60.0
_CONNECTION_IDLE_SECONDS = 600.0
_IDLE_CHECK_SECONDS = 30.0


class AdxRouterClient(AdxFileMixin, AgentServerClient):
    """Route trusted envelope identities to stable, deployment-selected Bindings.

    Only sockets are cached here. ADX owns activation and Environment lifecycle.
    A failed business request is returned to its caller without an automatic retry.
    """

    # File routes use the existing business callback, never ADX IAM identities.
    auth_enabled = True

    def __init__(
        self,
        config: AdxRouterConfig,
        *,
        ws_client_factory: Callable[[], WebSocketAgentServerClient] | None = None,
        control_client: AdxControlClient | None = None,
    ) -> None:
        self._config = config
        self._control = control_client or AdxControlClient(config)
        self._deleting: set[str] = set()
        self._factory = ws_client_factory or WebSocketAgentServerClient
        self._clients: dict[str, WebSocketAgentServerClient] = {}
        self._connecting: dict[str, asyncio.Task] = {}
        self._pool_lock = asyncio.Lock()
        self._active_requests: dict[str, int] = {}
        self._frontend_connections: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._pending_cleanups: dict[str, asyncio.Task] = {}
        self._idle_reaper_task: asyncio.Task | None = None
        self._closed = True
        self._web_channel = None
        self._push_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    def server_ready(self) -> bool:
        # Front transport is selected per user on demand, not at Gateway startup.
        return not self._closed

    async def connect(self, uri: str) -> None:
        """Enable lazy per-user connections without creating a default environment."""
        del uri
        self._closed = False
        if self._idle_reaper_task is None or self._idle_reaper_task.done():
            self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

    async def disconnect(self) -> None:
        self._closed = True
        tasks = list(self._connecting.values()) + list(self._pending_cleanups.values())
        if self._idle_reaper_task is not None:
            tasks.append(self._idle_reaper_task)
        self._idle_reaper_task = None
        self._pending_cleanups.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        clients = list(self._clients.values())
        self._clients.clear()
        self._last_used.clear()
        await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)

    async def shutdown(self) -> None:
        await self.disconnect()
        await self._control.close()
        self._frontend_connections.clear()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        """Startup defaults belong to the ADX Template; runtime config stays E2A-owned."""
        del config, env

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._push_handler = handler

    def set_channel_manager(self, channel_manager) -> None:
        """Attach file forwarding without changing existing channel authentication."""
        channel = channel_manager.get_channel(ChannelType.WEB)
        self._web_channel = channel
        if channel is not None:
            channel.container_file_client = self
        channel_manager.subscribe_channel_events(self._on_channel_event)

    async def _on_channel_event(self, event: Any) -> None:
        """Reuse Gateway connection events to protect online users' push sockets."""
        if self._closed:
            return
        if event.channel_type not in ("web", "tui") or not event.user_id:
            return
        user = event.user_id
        count = self._frontend_connections.get(user, 0)
        if event.event_type == "connected":
            self._frontend_connections[user] = count + 1
            self._cancel_cleanup(user)
        elif event.event_type == "disconnected":
            if count > 1:
                self._frontend_connections[user] = count - 1
            else:
                self._frontend_connections.pop(user, None)
                if user in self._clients:
                    self._cancel_cleanup(user)
                    self._pending_cleanups[user] = asyncio.create_task(self._cleanup_disconnected(user))

    def _cancel_cleanup(self, user: str) -> None:
        task = self._pending_cleanups.pop(user, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _cleanup_disconnected(self, user: str) -> None:
        try:
            await asyncio.sleep(_DISCONNECT_CLEANUP_SECONDS)
            async with self._pool_lock:
                if self._closed or not self._can_reclaim(user):
                    return
                client = self._clients.get(user)
                if client is not None:
                    await self._evict(user, client)
        except Exception:
            logger.exception("ADX disconnected connection cleanup failed")
        finally:
            if self._pending_cleanups.get(user) is asyncio.current_task():
                del self._pending_cleanups[user]

    async def _idle_reaper_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(_IDLE_CHECK_SECONDS)
            try:
                await self._reap_idle_once()
            except Exception:
                logger.exception("ADX idle connection cleanup failed")

    async def _reap_idle_once(self) -> None:
        async with self._pool_lock:
            for user, client in list(self._clients.items()):
                if self._clients.get(user) is not client or not self._can_reclaim(user):
                    continue
                if time.monotonic() - self._last_used[user] >= _CONNECTION_IDLE_SECONDS:
                    await self._evict(user, client)

    def _user_online(self, user: str) -> bool:
        if self._frontend_connections.get(user, 0):
            return True
        # Web can accept sockets before the router subscribes during startup.
        channel = self._web_channel
        return channel is not None and any(
            channel.connection_user_id(ws) == user for ws in channel.clients
        )

    def _can_reclaim(self, user: str) -> bool:
        return not self._active_requests.get(user, 0) and not self._user_online(user)

    async def authenticate_http(
        self,
        *,
        path: str,
        headers: Mapping[str, str],
        remote: str = "",
        channel: str = "",
        allow_query_token: bool = False,
    ) -> AuthResult:
        """Require the Web channel's business authentication for file requests."""
        if channel == "file-api":
            self._begin_file_transfer()
        if self._web_channel is None:
            return AuthResult(success=False, error="Business authentication is not configured")
        result = await self._web_channel.authenticate_handshake(
            path=path if allow_query_token else urlsplit(path).path,
            headers=headers,
            remote=remote,
            channel=channel,
        )
        if result is None:
            return AuthResult(success=False, error="Business authentication is not configured")
        if result.success and not str(result.user_id or "").strip():
            return AuthResult(success=False, error="Business authentication did not provide a user identity")
        return result

    def _user(self, envelope: E2AEnvelope) -> str:
        if envelope.channel not in (None, "", "web", "tui", "__cron__"):
            raise AdxRoutingError("Unsupported ADX channel", code="ADX_CHANNEL_UNSUPPORTED")
        user = envelope.user_id
        if not isinstance(user, str) or not user.strip() or user != user.strip():
            raise AdxRoutingError("A Jiuwen business user_id is required", code="ADX_USER_REQUIRED")
        if len(user.encode("utf-8")) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in user):
            raise AdxRoutingError("A Jiuwen business user_id is required", code="ADX_USER_REQUIRED")
        params = envelope.params
        if params.get("user_id") not in (None, "", user):
            raise AdxRoutingError("User identity mismatch", code="ADX_USER_MISMATCH")
        if params.get("agent_type") not in (None, "", self._config.agent_type):
            raise AdxRoutingError("Unsupported agent_type", code="ADX_AGENT_UNSUPPORTED")
        return user

    def _uri(self, user: str) -> str:
        config = self._config
        target = "urn:adx:binding:" + ":".join(
            quote(part, safe="") for part in (config.template, config.version, config.binding_id(user))
        )
        query = {"target": target}
        if config.service_port is not None:
            query["port"] = str(config.service_port)
        return urlunsplit(("wss", urlsplit(config.ingress_url).netloc, "/agent/ws", urlencode(query), ""))

    async def _evict(self, user: str, client: WebSocketAgentServerClient) -> None:
        # A late failure on an old socket cannot remove its replacement.
        if self._clients.get(user) is client:
            del self._clients[user]
            self._last_used.pop(user, None)
            self._cancel_cleanup(user)
            await client.disconnect()

    async def _open(self, user: str) -> WebSocketAgentServerClient:
        client = None

        async def on_disconnect(exc: BaseException) -> None:
            del exc
            await self._evict(user, client)

        async def on_push(wire: dict[str, Any]) -> None:
            if self._closed or self._clients.get(user) is not client or self._push_handler is None:
                return
            await self._push_handler(wire)

        try:
            client = self._factory()
            client.set_disconnect_handler(on_disconnect)
            client.set_server_push_handler(on_push)
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                delay = 0.25
                while True:
                    try:
                        await client.connect(
                            self._uri(user), extra_headers={
                                "Authorization": f"Bearer {self._config.token}",
                                "X-ADX-Parameters": urlencode(self._control.parameters(user)),
                            },
                        )
                        break
                    except (InvalidHandshake, TimeoutError) as exc:
                        if isinstance(exc, InvalidHandshake):
                            response = getattr(exc, "response", None)
                            status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
                            if status not in (502, 503, 504):
                                raise exc
                        # A cold Environment can be selected before its WS port accepts
                        # connections, or activation can outlast the WS handshake timer.
                        # No E2A request has been sent during a handshake.
                        await client.disconnect()
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 2.0)
            if self._closed or not client.server_ready:
                raise AdxRoutingError("ADX AgentServer handshake is unavailable", code="ADX_CONNECT_FAILED")
            self._clients[user] = client
            self._last_used[user] = time.monotonic()
            return client
        except BaseException:
            if client is not None:
                await client.disconnect()
            raise
        finally:
            if self._connecting.get(user) is asyncio.current_task():
                del self._connecting[user]

    async def _get_client(self, user: str) -> WebSocketAgentServerClient:
        async with self._pool_lock:
            if self._closed:
                raise AdxRoutingError("ADX router is closed", code="ADX_ROUTER_CLOSED")
            if user in self._deleting:
                raise AdxRoutingError("ADX Binding is being rebuilt", code="ADX_BINDING_REBUILDING")
            existing = self._clients.get(user)
            if existing is not None:
                if existing.server_ready:
                    self._last_used[user] = time.monotonic()
                    return existing
                await self._evict(user, existing)
            task = self._connecting.get(user)
            if task is None:
                if len(self._clients) + len(self._connecting) >= self._config.max_connections:
                    for idle_user in sorted(self._clients, key=lambda candidate: self._last_used[candidate]):
                        if not self._can_reclaim(idle_user):
                            continue
                        await self._evict(idle_user, self._clients[idle_user])
                        break
                if self._closed:
                    raise AdxRoutingError("ADX router is closed", code="ADX_ROUTER_CLOSED")
                if len(self._clients) + len(self._connecting) >= self._config.max_connections:
                    raise AdxRoutingError("ADX connection capacity reached", code="ADX_CAPACITY_EXCEEDED")
                task = asyncio.create_task(self._open(user))
                # Retrieve abandoned task failures when an individual waiter is cancelled.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self._connecting[user] = task
        try:
            return await asyncio.shield(task)
        except Exception as exc:
            raise AdxRoutingError("ADX AgentServer connection failed", code="ADX_CONNECT_FAILED") from exc

    @asynccontextmanager
    async def _connection(self, user: str) -> AsyncIterator[WebSocketAgentServerClient]:
        """Hold a connection through an entire request, including stream cleanup."""
        self._active_requests[user] = self._active_requests.get(user, 0) + 1
        client = None
        try:
            client = await self._get_client(user)
            yield client
        finally:
            remaining = self._active_requests[user] - 1
            if remaining:
                self._active_requests[user] = remaining
            else:
                del self._active_requests[user]
            if self._clients.get(user) is client and client is not None:
                self._last_used[user] = time.monotonic()

    async def delete_agent(self, user_id: str, agent_type: str | None = None) -> dict[str, Any]:
        self._user(E2AEnvelope(user_id=user_id, channel="web", params={"agent_type": agent_type}))
        if user_id in self._deleting:
            raise AdxRoutingError("ADX Binding is being rebuilt", code="ADX_BINDING_REBUILDING")
        self._deleting.add(user_id)
        try:
            opening = self._connecting.get(user_id)
            if opening:
                # Keep a possibly committed WS-triggered activation ordered before deletion.
                await asyncio.gather(asyncio.shield(opening), return_exceptions=True)
            client = self._clients.get(user_id)
            if client:
                await self._evict(user_id, client)
            await self._control.delete_binding(user_id)
            return {"success": True}
        finally:
            self._deleting.discard(user_id)

    async def send_request(
        self, envelope: E2AEnvelope, *, timeout: float | None = None,
    ) -> AgentResponse:
        if envelope.channel == HEALTH_CHECK_CHANNEL_ID:
            if self._closed:
                raise AdxRoutingError("ADX router is closed", code="ADX_ROUTER_CLOSED")
            # Temporary mock: do not activate a sandbox for Gateway's userless probe.
            return AgentResponse(
                envelope.request_id,
                envelope.channel,
                payload={"health_check": f"{HEALTH_CHECK_OK}: mocked by adx_router"},
            )
        user = self._user(envelope)
        async with self._connection(user) as client:
            try:
                return await client.send_request(replace(envelope, params=dict(envelope.params)), timeout=timeout)
            except (AgentServerUnaryTimeout, DuplicateRequestIdError):
                raise
            except (OSError, RuntimeError) as exc:
                await self._evict(user, client)
                raise AdxRoutingError(
                    "ADX connection lost; request outcome is unknown and was not replayed", code="ADX_CONNECTION_LOST",
                ) from exc

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[AgentResponseChunk]:
        user = self._user(envelope)
        async with self._connection(user) as client:
            try:
                async for chunk in client.send_request_stream(replace(envelope, params=dict(envelope.params))):
                    yield chunk
            except DuplicateRequestIdError:
                raise
            except (OSError, RuntimeError) as exc:
                await self._evict(user, client)
                raise AdxRoutingError(
                    "ADX connection lost; stream outcome is unknown and was not replayed", code="ADX_CONNECTION_LOST",
                ) from exc
