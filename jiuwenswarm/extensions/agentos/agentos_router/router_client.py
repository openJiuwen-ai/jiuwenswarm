# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Mapping, Coroutine

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    BUILTIN_AGENT_TYPE,
    AgentCreateFailed,
    AgentCreatingTimeout,
    AgentDeleted,
    AgentManager,
    AgentRuntime,
    is_third_party_agent_type,
)
from jiuwenswarm.extensions.agentos.agentos_router.agentos_authenticator import AgentOSAuthenticator
from jiuwenswarm.extensions.agentos.auth.common import (
    apply_auth_result_to_ws,
    extract_headers,
    extract_token,
    get_remote_addr,
)
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthContext, AuthResult
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    DEFAULT_AGENT_WORKSPACE_ROOT,
    SshChannelEndpoint,
)
from jiuwenswarm.extensions.agentos.agentos_router.models import (
    AgentInfo,
    AgentStatus,
    ImageInfo,
)
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    RegistryClient,
    instance_service_id,
)
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import YuanrongSshRelay
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    AgentRuntimeSpec,
    YuanrongFrontendAgentClient,
)
from jiuwenswarm.extensions.agentos.auth.ssh_key_issuer import SshKeyIssuer
from jiuwenswarm.gateway import ChannelManager
from jiuwenswarm.gateway.channel_manager.base import ChannelType
from jiuwenswarm.gateway.routing.agent_client import (
    AgentServerClient,
    WebSocketAgentServerClient,
)


logger = logging.getLogger(__name__)

_WORKSPACE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# create_sandbox 返回后 agentserver 仍在进程内启动；YuanRong WS 代理此时会回
# HTTP 502。在 deadline 内重试，避免首条 chat 立刻空失败（TUI "Worked for 0s"）。
_WS_CONNECT_READY_TIMEOUT_SECONDS = 60.0
_WS_CONNECT_RETRY_INTERVAL_SECONDS = 1.0
_WS_CONNECT_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})
_WS_CONNECT_RETRYABLE_TEXT_TOKENS = (
    "http 502",
    "http 503",
    "http 504",
    "connection refused",
    "temporarily unavailable",
)


def _is_ws_connect_retryable(exc: BaseException) -> bool:
    """冷启动期间 proxy/agentserver 未就绪的可重试错误."""
    status = getattr(exc, "status_code", None)
    if status in _WS_CONNECT_RETRYABLE_HTTP_STATUS:
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    text = str(exc).lower()
    for token in _WS_CONNECT_RETRYABLE_TEXT_TOKENS:
        if token in text:
            return True
    return False


class UnsupportedAgentType(ValueError):
    pass


class EphemeralKeyIssueError(RuntimeError):
    """A configured issuer could not mint an ephemeral SSH key."""


def build_inline_runtime_spec(image_info: ImageInfo) -> AgentRuntimeSpec:
    """Take registry ``metadata.runtime_spec`` (YuanRong ``RuntimeSpec`` shape)."""
    meta = image_info.metadata if isinstance(image_info.metadata, dict) else {}
    raw_spec = meta.get("runtime_spec")
    if not isinstance(raw_spec, Mapping) or not raw_spec:
        raise ValueError(
            f"runtime_spec is required from registry for agent_type={image_info.image_name}"
        )
    return dict(raw_spec)  # type: ignore[return-value]


def _extract_runtime_spec_port(runtime_spec: Mapping[str, Any]) -> int | None:
    """从 runtime_spec ``rootfs.ports``（如 ``["tcp:18092"]``）取第一个端口."""
    rootfs = runtime_spec.get("rootfs")
    ports = rootfs.get("ports") if isinstance(rootfs, Mapping) else None
    if not isinstance(ports, (list, tuple)):
        return None
    for entry in ports:
        text = str(entry or "")
        candidate = text.rsplit(":", 1)[-1] if ":" in text else text
        try:
            return int(candidate)
        except ValueError:
            continue
    return None


def resolve_agent_workspace(user_id: str, *, workspace_root: str | None = None) -> str:
    """Resolve host workspace bind path for one agent user.

    Default: ``/home/agentos/users/<user_id>``. Optional ``workspace_root``
    overrides the parent directory (``{workspace_root}/<user_id>``).

    Best-effort ``mkdir``: permission errors are ignored so callers (and unit
    tests) can still pass the path to YuanRong create; the host/deploy side
    remains responsible for a writable mount source.
    """
    safe_user = _WORKSPACE_NAME_RE.sub("_", str(user_id or "").strip()) or "default"
    root = Path(workspace_root or DEFAULT_AGENT_WORKSPACE_ROOT).expanduser()
    workspace = (root / safe_user).resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[AgentOSRouter] workspace mkdir skipped: path=%s error=%s",
            workspace,
            exc,
        )
    return str(workspace)


class AgentOSRouterClient(AgentServerClient):
    """AgentServerClient implementation backed by YuanRong and AgentManager."""

    def __init__(
        self,
        yuanrong: YuanrongFrontendAgentClient,
        registry: RegistryClient,
        agent_manager: AgentManager,
        ssh_relay: YuanrongSshRelay | None = None,
        ssh_channel_endpoint: SshChannelEndpoint | None = None,
        key_issuer: SshKeyIssuer | None = None,
        ephemeral_key_ttl_sec: float = 300.0,
        workspace_root: str = DEFAULT_AGENT_WORKSPACE_ROOT,
        sandbox_idle_timeout_seconds: float = 600.0,
        sandbox_idle_check_interval_seconds: float = 30.0,
        auth_client: AgentOSAuthenticator | None = None,
        ws_client_factory: Callable[[], WebSocketAgentServerClient] | None = None,
    ) -> None:
        self._yuanrong = yuanrong
        self._registry = registry
        self._agent_manager = agent_manager
        self._ssh_relay = ssh_relay
        self._ssh_channel_endpoint = ssh_channel_endpoint
        self._key_issuer = key_issuer
        self._ephemeral_key_ttl_sec = float(ephemeral_key_ttl_sec)
        self._workspace_root = (
            str(workspace_root or "").strip() or DEFAULT_AGENT_WORKSPACE_ROOT
        )
        # <= 0 disables idle sandbox reclamation entirely.
        self._sandbox_idle_timeout_seconds = float(sandbox_idle_timeout_seconds)
        self._sandbox_idle_check_interval_seconds = max(
            1.0, float(sandbox_idle_check_interval_seconds)
        )
        self._idle_reaper_task: asyncio.Task[None] | None = None
        self._server_ready = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        # 用户当前 agent_type（3rdagent.switch 成功后更新）；SSH 接入跟随此值。
        self._current_agent_types: dict[str, str] = {}
        self._auth_client = auth_client
        # 延迟清理任务：user_id → pending cleanup task
        self._pending_cleanups: dict[str, asyncio.Task[None]] = {}
        # create 后走 YuanRong frontend 的 WS 代理直连 instance（不走 invoke 链路）：
        # instance_id(sandbox_id) → 已连接的 WebSocketAgentServerClient
        self._ws_clients: dict[str, WebSocketAgentServerClient] = {}
        self._ws_clients_lock = asyncio.Lock()
        # instance_id → 正在进行的 connect Future（合并并发首连，避免多路同时打 502）
        self._ws_connecting: dict[str, asyncio.Future[WebSocketAgentServerClient]] = {}
        self._ws_client_factory = ws_client_factory or WebSocketAgentServerClient
        self._push_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None


    def set_channel_manager(self, channel_manager: ChannelManager) -> None:
        """Subscribe TUI connect hooks (token auth) and channel disconnect events.

        Web is intentionally not hooked: browser WS cannot send Authorization
        headers, and the stock Web UI does not pass ``?token=``.
        """
        tui_channel = channel_manager.get_channel(ChannelType.CLI)

        if tui_channel:
            on_connect = getattr(tui_channel, "on_connect", None)
            if callable(on_connect):
                on_connect(self.on_connect)

        channel_manager.subscribe_channel_events(self._on_channel_event)

    async def on_connect(self, ws: Any) -> AuthResult | None:
        if self._auth_client is None:
            # auth 未启用：回落使用握手头里的 X-User-Id，并经
            # apply_auth_result_to_ws 显式落盘，保证连接计数/延迟清理
            # 与 chat 路由读取到同一身份。
            headers = {k.lower(): v for k, v in extract_headers(ws).items()}
            fallback_user_id = str(headers.get("x-user-id", "") or "").strip()
            result = AuthResult(
                success=True,
                user_id=fallback_user_id,
            )
            apply_auth_result_to_ws(ws, result)
            return result
        token = extract_token(ws)
        headers = extract_headers(ws)
        context = AuthContext(
            channel_type="",
            credentials={"token": token} if token else {},
            headers=headers,
            remote_addr=get_remote_addr(ws),
        )
        result = await self._auth_client.authenticate(context)
        if result.success:
            # 用 IAM 鉴权身份覆写 ws 上的自报 X-User-Id，
            # 连接事件上报与 chat 路由统一使用权威身份。
            apply_auth_result_to_ws(ws, result)
        else:
            close = getattr(ws, "close", None)
            if callable(close):
                ret = close(code=1008, reason="unauthorized")
                if hasattr(ret, "__await__"):
                    await ret
        return result

    def set_key_issuer(
        self,
        key_issuer: SshKeyIssuer | None,
        *,
        ephemeral_key_ttl_sec: float = 300.0,
    ) -> None:
        """Inject or clear the northbound SSH ephemeral key issuer."""
        self._key_issuer = key_issuer
        self._ephemeral_key_ttl_sec = float(ephemeral_key_ttl_sec)

    async def _on_channel_event(self, event: Any) -> None:
        """处理 Channel 连接事件，维护用户连接计数并触发延迟清理。"""
        user_id = str(getattr(event, "user_id", "") or "").strip()
        if not user_id:
            return
        event_type = str(getattr(event, "event_type", "") or "").strip()
        if event_type == "connected":
            self._agent_manager.increment_user_connections(user_id)
            # 取消可能挂起的延迟清理
            task = self._pending_cleanups.pop(user_id, None)
            if task is not None and not task.done():
                task.cancel()
        elif event_type == "disconnected":
            count = self._agent_manager.decrement_user_connections(user_id)
            if count <= 0:
                # 连接数为 0：1 分钟后触发 jiuwenswarm agent 清理
                task = asyncio.create_task(
                    self._delayed_cleanup(user_id),
                    name=f"agentos-delayed-cleanup-{user_id[:24]}",
                )
                self._pending_cleanups[user_id] = task
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _delayed_cleanup(self, user_id: str) -> None:
        """连接断开 1 分钟后，若用户仍无连接，删除其 jiuwenswarm agent。"""
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        # 二次检查：用户可能已重连
        if self._agent_manager.get_user_connection_count(user_id) > 0:
            return
        try:
            runtimes = await self._agent_manager.list_user_agents(user_id)
        except Exception:
            logger.exception(
                "[AgentOSRouter] delayed cleanup list_user_agents failed: user=%s",
                user_id,
            )
            return
        for runtime in runtimes:
            if runtime.info.agent_type != BUILTIN_AGENT_TYPE:
                continue
            key_values: dict[str, Any] | None = None
            if "session_id" in self._agent_manager.key_fields:
                session_id = runtime.info.metadata.get("session_id", "")
                if session_id:
                    key_values = {"session_id": session_id}
            try:
                await self.delete_agent(user_id, runtime.info.agent_type, key_values=key_values)
                logger.info(
                    "[AgentOSRouter] delayed cleanup deleted agent: user=%s agent_type=%s",
                    user_id,
                    runtime.info.agent_type,
                )
            except Exception:
                logger.exception(
                    "[AgentOSRouter] delayed cleanup delete failed: user=%s agent_type=%s",
                    user_id,
                    runtime.info.agent_type,
                )

    def get_current_agent_type(self, user_id: str) -> str:
        """Return the user's current agent_type (default ``jiuwenswarm``)."""
        uid = str(user_id or "").strip()
        return self._current_agent_types.get(uid) or BUILTIN_AGENT_TYPE

    @staticmethod
    def _uses_direct_yuanrong(agent_type: str) -> bool:
        """Builtin swarm uses URN invoke (same as ``agent_client.type=yuanrong``)."""
        return str(agent_type or "").strip().lower() == BUILTIN_AGENT_TYPE

    @property
    def server_ready(self) -> bool:
        return self._server_ready and self._yuanrong.server_ready

    async def connect(self, uri: str) -> None:
        await self._yuanrong.connect(uri)
        self._closed = False
        self._server_ready = True
        self._ensure_idle_reaper_task()

    async def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server_ready = False
        await self._stop_idle_reaper_task()
        # 取消所有挂起的延迟清理任务
        for task in self._pending_cleanups.values():
            if not task.done():
                task.cancel()
        self._pending_cleanups.clear()
        await self._drain_background_tasks()
        await self._close_all_ws_clients()
        try:
            await self._yuanrong.disconnect()
        finally:
            await self._registry.close()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        self._yuanrong.set_or_update_server_config(config=config, env=env)

    def set_server_push_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._push_handler = handler
        setter = getattr(self._yuanrong, "set_server_push_handler", None)
        if callable(setter):
            setter(handler)
        for ws_client in self._ws_clients.values():
            ws_client.set_server_push_handler(handler)

    def _agent_ws_url(self, instance_id: str, agent_port: int) -> str:
        """YuanRong frontend 的 instance WS 代理地址.

        形如 ``ws://<frontend-host>:8888/serverless/v1/ws?instance=<id>&tenant_id=default&port=<port>``，
        其中 ``instance`` 是 create 返回的 instanceID，``port`` 是 create cmds 里
        agentserver 监听的端口。
        """
        frontend = str(self._yuanrong.frontend_endpoint or "").rstrip("/")
        parsed = urllib.parse.urlsplit(frontend)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode(
            {
                "instance": instance_id,
                "tenant_id": self._yuanrong.agent_namespace or "default",
                "port": str(agent_port),
            }
        )
        return f"{ws_scheme}://{parsed.netloc}/serverless/v1/ws?{query}"

    async def _connect_ws_until_ready(
        self,
        *,
        instance_id: str,
        agent_port: int,
    ) -> WebSocketAgentServerClient:
        """建立到 instance 的 WS；对冷启动 502 等做 deadline 内重试."""
        uri = self._agent_ws_url(instance_id, agent_port)
        deadline = asyncio.get_running_loop().time() + _WS_CONNECT_READY_TIMEOUT_SECONDS
        attempt = 0

        while True:
            attempt += 1
            client = self._ws_client_factory()
            if self._push_handler is not None:
                client.set_server_push_handler(self._push_handler)
            logger.info(
                "[AgentOSRouter] connecting agent instance via ws: "
                "instance=%s attempt=%s uri=%s",
                instance_id,
                attempt,
                uri,
            )
            try:
                await client.connect(uri)
                if attempt > 1:
                    logger.info(
                        "[AgentOSRouter] agent ws ready after retry: "
                        "instance=%s attempts=%s",
                        instance_id,
                        attempt,
                    )
                return client
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    logger.warning(
                        "[AgentOSRouter] cleanup after failed ws connect: "
                        "instance=%s attempt=%s",
                        instance_id,
                        attempt,
                        exc_info=True,
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or not _is_ws_connect_retryable(exc):
                    raise
                sleep_for = min(_WS_CONNECT_RETRY_INTERVAL_SECONDS, remaining)
                logger.warning(
                    "[AgentOSRouter] agent ws not ready, retrying: "
                    "instance=%s attempt=%s sleep=%.1fs error=%s",
                    instance_id,
                    attempt,
                    sleep_for,
                    exc,
                )
                await asyncio.sleep(sleep_for)

    async def _get_ws_client(self, runtime: AgentRuntime) -> WebSocketAgentServerClient:
        """获取（或建立）到该 agent instance 的 WS 直连，不走 invoke 链路.

        冷启动时 create 返回早于 agentserver listen：对可重试错误做就绪等待。
        同一 instance 的并发首连合并到一个 Future，避免多路同时打 502。
        """
        info = runtime.info
        instance_id = str(info.sandbox_id or "").strip()
        if not instance_id:
            raise ValueError(
                f"agent has no sandbox instance for ws connect: "
                f"user={info.user_id} agent_type={info.agent_type}"
            )
        raw_port = info.metadata.get("agent_port")
        try:
            agent_port = int(raw_port)
        except (TypeError, ValueError):
            raise ValueError(
                f"agent has no agent_port metadata for ws connect: "
                f"instance={instance_id} agent_type={info.agent_type}"
            ) from None

        async with self._ws_clients_lock:
            existing = self._ws_clients.get(instance_id)
            if existing is not None:
                return existing
            inflight = self._ws_connecting.get(instance_id)
            if inflight is None:
                inflight = asyncio.get_running_loop().create_future()
                self._ws_connecting[instance_id] = inflight
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            return await asyncio.shield(inflight)

        try:
            client = await self._connect_ws_until_ready(
                instance_id=instance_id,
                agent_port=agent_port,
            )
        except Exception as exc:
            async with self._ws_clients_lock:
                self._ws_connecting.pop(instance_id, None)
                if not inflight.done():
                    inflight.set_exception(exc)
            raise

        async with self._ws_clients_lock:
            self._ws_clients[instance_id] = client
            self._ws_connecting.pop(instance_id, None)
            if not inflight.done():
                inflight.set_result(client)
        return client

    async def _close_ws_client(self, instance_id: str | None) -> None:
        if not instance_id:
            return
        key = str(instance_id)
        async with self._ws_clients_lock:
            client = self._ws_clients.pop(key, None)
            inflight = self._ws_connecting.pop(key, None)
        if inflight is not None and not inflight.done():
            inflight.cancel()
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.warning(
                "[AgentOSRouter] close agent ws failed: instance=%s",
                instance_id,
                exc_info=True,
            )

    async def _close_all_ws_clients(self) -> None:
        async with self._ws_clients_lock:
            clients = list(self._ws_clients.values())
            self._ws_clients.clear()
            inflight = list(self._ws_connecting.values())
            self._ws_connecting.clear()
        for fut in inflight:
            if not fut.done():
                fut.cancel()
        for client in clients:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("[AgentOSRouter] close agent ws failed", exc_info=True)

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        # 3rdagent.list / 3rdagent.switch are handled by Gateway ThirdAgent
        # (TUI local_handler), not via E2A send_request.
        if self._is_ssh_relay_request(envelope):
            return await self._handle_ssh_relay(envelope)
        try:
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed) as exc:
            return self._routing_error_response(envelope, str(exc))
        try:
            runtime.attach_to_envelope(envelope)
            # create 后通过 YuanRong frontend WS 代理直连 instance，不走 invoke。
            try:
                ws_client = await self._get_ws_client(runtime)
            except ValueError as exc:
                return self._routing_error_response(envelope, str(exc))
            return await ws_client.send_request(envelope)
        finally:
            await self._agent_manager.release(runtime.key)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        try:
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed) as exc:
            yield self._routing_error_chunk(envelope, str(exc))
            return
        try:
            runtime.attach_to_envelope(envelope)
            # create 后通过 YuanRong frontend WS 代理直连 instance，不走 invoke。
            try:
                ws_client = await self._get_ws_client(runtime)
            except ValueError as exc:
                yield self._routing_error_chunk(envelope, str(exc))
                return
            async for chunk in ws_client.send_request_stream(envelope):
                yield chunk
        finally:
            await self._agent_manager.release(runtime.key)

    async def thirdagent_list(
        self,
        *,
        user_id: str,
        current_agent_type: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.list``: list switchable third-party agent images."""
        uid = str(user_id or "").strip()
        if not uid:
            return {
                "ok": False,
                "error": "user_id is required for AgentOS routing",
                "code": "BAD_REQUEST",
            }
        images = await self._registry.list_user_images(uid)
        agents: list[dict[str, Any]] = []
        for image in images:
            agent_type = str(
                (image.metadata or {}).get("agent_type") or image.image_name or ""
            ).strip()
            if not agent_type:
                continue
            agents.append(
                {
                    "agent_type": agent_type,
                    "image_name": image.image_name,
                    "image_uri": image.image_uri,
                    "metadata": dict(image.metadata or {}),
                }
            )
        current = (
            str(current_agent_type or "").strip()
            or self.get_current_agent_type(uid)
        )
        return {
            "ok": True,
            "payload": {
                "agents": agents,
                "current_agent_type": current,
            },
        }

    def _ssh_endpoint_fields(self) -> dict[str, Any] | None:
        """Northbound ``channels.ssh`` listen ip/port, or None if unavailable."""
        endpoint = self._ssh_channel_endpoint
        if endpoint is None:
            return None
        ip = str(endpoint.ip or "").strip()
        port = int(endpoint.port or 0)
        if not ip or port <= 0:
            return None
        return {"ssh_ip": ip, "ssh_port": port}

    @staticmethod
    def _missing_ssh_endpoint_error() -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                "ssh channel endpoint is unavailable: enable channels.ssh "
                "and set listen_host / listen_port"
            ),
            "code": "SSH_ENDPOINT_UNAVAILABLE",
        }

    async def thirdagent_switch(
        self,
        *,
        user_id: str,
        agent_type: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.switch``: ensure agent exists without forwarding chat.

        Success payload includes northbound SSH channel ``ssh_ip``/``ssh_port``
        (``channels.ssh.listen_host`` / ``listen_port``). Missing values fail.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {
                "ok": False,
                "error": "user_id is required for AgentOS routing",
                "code": "BAD_REQUEST",
            }
        try:
            normalized = AgentRuntime.normalize_agent_type(agent_type)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "UNSUPPORTED_AGENT_TYPE",
            }
        # Fail fast before create when northbound SSH channel is not configured.
        ssh_fields = self._ssh_endpoint_fields()
        if ssh_fields is None:
            return self._missing_ssh_endpoint_error()
        # Fail fast before create: without the key the client cannot pass
        # SSH public-key auth, so a "successful" switch would be unusable.
        try:
            key_fields = self._ephemeral_ssh_key_fields(
                user_id=uid,
                session_id=session_id,
            )
        except EphemeralKeyIssueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "SSH_KEY_ISSUE_FAILED",
            }
        # Builtin swarm: no registry / create_sandbox; mark current type only.
        if self._uses_direct_yuanrong(normalized):
            self._current_agent_types[uid] = normalized
            payload = {
                "agent_id": "",
                "agent_type": normalized,
                "sandbox_id": "",
                "status": AgentStatus.READY.value,
                **ssh_fields,
                **key_fields,
            }
            return {"ok": True, "payload": payload}
        try:
            runtime = await self._agent_manager.get_or_create_agent(
                uid,
                normalized,
                key_values={"session_id": session_id} if session_id else None,
                creator=self._create_agent,
                metadata={"session_id": session_id} if session_id else None,
            )
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed) as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "INTERNAL_ERROR",
            }
        info = runtime.info
        status = info.status.value if hasattr(info.status, "value") else str(info.status)
        instance_id = str(info.sandbox_id or "").strip()
        ssh_relay = self._ssh_relay
        if ssh_relay is not None:
            if not instance_id:
                return {
                    "ok": False,
                    "error": f"agent has no yuanrong instance_id: user={uid}",
                    "code": "INTERNAL_ERROR",
                }
            try:
                # create 返回不代表 sshd 已听端口；等南向 SSH 通了再让客户端连。
                await ssh_relay.wait_until_ready(instance_id, user_id=uid)
            except Exception as exc:
                logger.warning(
                    "[AgentOSRouter] 3rdagent.switch sshd not ready: "
                    "user=%s instance=%s error=%s",
                    uid,
                    instance_id,
                    exc,
                )
                return {
                    "ok": False,
                    "error": f"sandbox sshd not ready: {exc}",
                    "code": "SSH_NOT_READY",
                }
        # 记录用户当前 agent_type，后续 SSH 接入默认跟随
        self._current_agent_types[uid] = normalized
        payload = {
            "agent_id": info.agent_id,
            "agent_type": info.agent_type,
            "sandbox_id": info.sandbox_id,
            "status": status,
            **ssh_fields,
            **key_fields,
        }
        return {"ok": True, "payload": payload}

    def _ephemeral_ssh_key_fields(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Mint ``ssh_private_key`` when an issuer is configured.

        Returns an empty mapping when no issuer is injected (auth disabled).
        Raises :class:`EphemeralKeyIssueError` when issuance is expected but
        does not yield a usable key.
        """
        issuer = self._key_issuer
        if issuer is None:
            return {}
        try:
            private_key = issuer.issue_ephemeral_key(
                user_id=user_id,
                username=user_id,
                session_id=str(session_id or ""),
                ttl_sec=self._ephemeral_key_ttl_sec,
            )
        except Exception as exc:
            logger.error(
                "[AgentOSRouter] failed to issue ephemeral SSH key: user=%s error=%s",
                user_id,
                exc,
            )
            raise EphemeralKeyIssueError(
                f"failed to issue ephemeral SSH key: {exc}"
            ) from exc
        if not private_key:
            logger.error(
                "[AgentOSRouter] ephemeral SSH key issuer returned an empty key: user=%s",
                user_id,
            )
            raise EphemeralKeyIssueError("ephemeral SSH key issuer returned an empty key")
        return {"ssh_private_key": private_key}

    async def shutdown(self) -> None:
        try:
            await self.disconnect()
        finally:
            auth_client = self._auth_client
            close = getattr(auth_client, "aclose", None)
            if callable(close):
                await close()

    # ---------- SSH relay (northbound SshChannel -> YuanRong instance) ----------

    @staticmethod
    def _is_ssh_relay_request(envelope: E2AEnvelope) -> bool:
        return str(envelope.method or "") == ReqMethod.SSH_RELAY.value

    async def _handle_ssh_relay(self, envelope: E2AEnvelope) -> AgentResponse:
        """Start the southbound SSH relay for an ``ssh.relay`` request.

        Agent resolution (YuanRong instance creation) and the PTY relay run
        in a background task so the gateway forward loop is not blocked for
        the whole SSH session; the northbound channel waits on the relay
        session ``done`` event instead of this response.
        """
        session_id = str(envelope.session_id or "")
        params = envelope.params if isinstance(envelope.params, dict) else {}
        # Live SshRelaySession handed over in-process by the northbound
        # SshChannel; pop it so it never leaks into serialization/logging.
        relay_session = params.pop("relay_session", None)
        if relay_session is None:
            return self._routing_error_response(
                envelope, f"ssh relay session not found in params: {session_id}"
            )
        if self._ssh_relay is None:
            msg = "ssh relay is not configured for AgentOS router"
            relay_session.exit_code = 1
            relay_session.done.set()
            return self._routing_error_response(envelope, msg)

        task = asyncio.create_task(
            self._run_ssh_relay(envelope, relay_session),
            name=f"agentos-ssh-relay-{session_id[:24]}",
        )
        relay_session.relay_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            ok=True,
            payload={"method": ReqMethod.SSH_RELAY.value, "status": "relay_started"},
        )

    async def _run_ssh_relay(self, envelope: E2AEnvelope, relay_session: Any) -> None:
        ssh_relay = self._ssh_relay
        if ssh_relay is None:
            # _handle_ssh_relay already guards this; keep a safe fallback.
            relay_session.exit_code = 1
            relay_session.done.set()
            return
        self._apply_current_agent_type_for_ssh(envelope)
        try:
            agent_type = self._extract_agent_type(envelope)
            if self._uses_direct_yuanrong(agent_type):
                ssh_relay.fail_session(
                    relay_session,
                    "builtin agent_type has no AgentOS sandbox for SSH; run "
                    "3rdagent.switch first or provide a remote command so "
                    "agent_type can be derived from its first token",
                )
                return
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed, AgentDeleted) as exc:
            ssh_relay.fail_session(
                relay_session, f"agent resolve failed: {exc}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - creation errors must release the client
            logger.exception(
                "[AgentOSRouter] ssh relay agent creation failed: session=%s",
                relay_session.session_id,
            )
            ssh_relay.fail_session(
                relay_session, f"agent creation failed: {exc}"
            )
            return

        # Hold the task count for the whole SSH session so the idle reaper
        # never reclaims a sandbox with a live (even silent) SSH connection.
        try:
            instance_id = str(runtime.info.sandbox_id or "").strip()
            if not instance_id:
                ssh_relay.fail_session(
                    relay_session,
                    f"agent has no yuanrong instance_id: user={runtime.info.user_id}",
                )
                return

            runtime.attach_to_envelope(envelope)
            logger.info(
                "[AgentOSRouter] ssh relay start: session=%s user=%s instance=%s",
                relay_session.session_id,
                runtime.info.user_id,
                instance_id,
            )
            await ssh_relay.run(
                relay_session,
                instance_id,
                user_id=runtime.info.user_id,
            )
        finally:
            await self._agent_manager.release(runtime.key)

    def _apply_current_agent_type_for_ssh(self, envelope: E2AEnvelope) -> None:
        """SSH 接入跟随用户当前 agent_type（由 3rdagent.switch 记录）。

        未 switch / 仍为内置 ``jiuwenswarm`` 时，取 SSH 远程指令首词作为
        agent_type。
        """
        params = envelope.params if isinstance(envelope.params, dict) else {}
        if str(params.get("agent_type") or "").strip():
            return
        user_id = str(envelope.user_id or "").strip()
        current = self.get_current_agent_type(user_id)
        if self._uses_direct_yuanrong(current):
            command = str(params.get("command") or "").strip()
            if not command:
                ctx = envelope.channel_context
                if isinstance(ctx, dict):
                    command = str(ctx.get("command") or "").strip()
            token = command.split(maxsplit=1)[0].lower() if command else ""
            current = token or BUILTIN_AGENT_TYPE
        params = dict(params)
        params["agent_type"] = current
        envelope.params = params
        logger.info(
            "[AgentOSRouter] ssh relay follows user current agent_type: "
            "user=%s agent_type=%s",
            user_id,
            current,
        )

    async def _drain_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _resolve_agent(
        self,
        envelope: E2AEnvelope,
        *,
        acquire: bool = False,
    ) -> AgentRuntime:
        user_id = self._extract_user_id(envelope)
        agent_type = self._extract_agent_type(envelope)
        return await self._agent_manager.get_or_create_agent(
            user_id,
            agent_type,
            key_values={"session_id": envelope.session_id},
            creator=self._create_agent,
            metadata={"session_id": envelope.session_id},
            acquire=acquire,
        )

    # ---------- idle sandbox reclamation ----------

    def _idle_reaper_enabled(self) -> bool:
        return self._sandbox_idle_timeout_seconds > 0

    def _ensure_idle_reaper_task(self) -> None:
        if self._closed or not self._idle_reaper_enabled():
            return
        if self._idle_reaper_task is not None and not self._idle_reaper_task.done():
            return
        self._idle_reaper_task = asyncio.create_task(
            self._idle_reaper_loop(),
            name="agentos-sandbox-idle-reaper",
        )

    async def _stop_idle_reaper_task(self) -> None:
        task = self._idle_reaper_task
        self._idle_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _idle_reaper_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._sandbox_idle_check_interval_seconds)
            try:
                await self._reap_idle_once()
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.exception("[AgentOSRouter] idle sandbox reap pass failed")

    async def _reap_idle_once(self) -> int:
        """Reclaim agents idle beyond the timeout; returns the reclaimed count.

        Delegates to :meth:`delete_agent` with ``idle_timeout_seconds`` so the
        same sandbox + registry cleanup path is used. ``pop_if_idle`` inside
        ``delete_agent`` re-checks READY / ``task_count == 0`` / staleness under
        the manager lock, so a concurrent acquire can never lose its sandbox.
        """
        if not self._idle_reaper_enabled():
            return 0
        reaped = 0
        for key in await self._agent_manager.list_keys():
            values = dict(zip(self._agent_manager.key_fields, key, strict=False))
            user_id = str(values.pop("user_id", "") or "").strip()
            agent_type = str(values.pop("agent_type", "") or "").strip()
            if not user_id or not agent_type:
                continue
            try:
                deleted = await self.delete_agent(
                    user_id,
                    agent_type,
                    key_values=values or None,
                    idle_timeout_seconds=self._sandbox_idle_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - keep reaping other agents
                logger.exception(
                    "[AgentOSRouter] delete idle agent failed: user=%s agent_type=%s",
                    user_id,
                    agent_type,
                )
                continue
            if deleted:
                reaped += 1
        return reaped

    async def _create_agent(self, agent_info: AgentInfo) -> AgentInfo:
        # runtime_spec 获取方式因 agent_type 而异
        if agent_info.agent_type == BUILTIN_AGENT_TYPE:
            # jiuwenswarm: 不从注册中心获取镜像信息，使用内置 runtime_spec
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            runtime_spec: dict[str, Any] = {
                "sandbox_type": "supervisor",
                "runtime": "python3.11",
                "rootfs": {
                    "imageurl": f"{BUILTIN_AGENT_TYPE}-agent-runtime:latest",
                    "user": "agentos",
                    "ports": [f"tcp:{port}"]
                },
                "cmds": [["sh", "-c", f"exec jiuwenswarm-agentserver --port {port}"]],
                "cpu": int(os.environ.get("AGENTOS_BUILTIN_AGENT_CPU", "2000")),
                "memory": int(os.environ.get("AGENTOS_BUILTIN_AGENT_MEMORY", "4096"))
            }
            env_vars = {"AGENT_SERVER_HOST": "127.0.0.1", "AGENT_SERVER_PORT": f"{port}"}
            # create 后 Gateway 通过 frontend WS 代理直连该端口（不走 invoke）。
            extra_metadata: dict[str, Any] = {"agent_port": port}
        else:
            image_info = await self._registry.get_image_info(agent_info.agent_type)
            runtime_spec = build_inline_runtime_spec(image_info)
            env_raw = image_info.metadata.get("env_vars")
            env_vars = (
                {str(k): str(v) for k, v in dict(env_raw).items()}
                if isinstance(env_raw, dict) and env_raw
                else None
            )
            extra_metadata = {"image_info": dict(image_info.metadata)}
            agent_port = _extract_runtime_spec_port(runtime_spec)
            if agent_port is not None:
                extra_metadata["agent_port"] = agent_port

        workspace = resolve_agent_workspace(
            agent_info.user_id,
            workspace_root=self._workspace_root,
        )
        sandbox = await self._yuanrong.create_sandbox(
            namespace=self._yuanrong.agent_namespace,
            name=f"{agent_info.user_id}+{agent_info.agent_type}",
            workspace=workspace,
            runtime_spec=runtime_spec,
            env_vars=env_vars,
        )
        instance_id = sandbox.sandbox_id
        agent_info.sandbox_id = instance_id
        agent_info.metadata.update(
            {
                "instance_id": instance_id,
                "workspace": workspace,
                "runtime_spec": dict(runtime_spec),
                **extra_metadata,
                "sandbox": dict(sandbox.metadata),
            }
        )
        agent_info.status = AgentStatus.READY

        task = asyncio.create_task(
            self._register_agent(agent_info.copy()),
            name=f"agentos-register-{agent_info.agent_id[:12]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return agent_info

    async def delete_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: dict[str, Any] | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> bool:
        """Delete agent mapping, release its YuanRong sandbox, unregister registry.

        When ``idle_timeout_seconds`` is set, only delete a READY agent that is
        unheld (``task_count == 0``) and idle beyond the timeout. Returns whether
        an agent was deleted.
        """
        resolved_key_values = dict(key_values or {})
        if idle_timeout_seconds is not None:
            return await self._delete_idle_agent(
                user_id,
                agent_type,
                key_values=resolved_key_values,
                idle_timeout_seconds=float(idle_timeout_seconds),
            )

        runtime = await self._agent_manager.get_agent(
            user_id, agent_type, key_values=resolved_key_values or None
        )
        if runtime is None:
            return False
        agent_info = runtime.info
        if (
            "session_id" not in resolved_key_values
            and agent_info.metadata.get("session_id")
        ):
            resolved_key_values["session_id"] = agent_info.metadata.get(
                "session_id"
            )
        await self._close_ws_client(agent_info.sandbox_id)
        if agent_info.sandbox_id:
            await self._yuanrong.delete_sandbox(agent_info.sandbox_id)
        await self._agent_manager.delete_agent(
            agent_info.user_id,
            agent_info.agent_type,
            key_values=resolved_key_values or None,
        )
        await self._unregister_agent(agent_info)
        return True

    async def _delete_idle_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: dict[str, Any],
        idle_timeout_seconds: float,
    ) -> bool:
        """Atomically pop an idle agent then run shared delete cleanup."""
        key = AgentRuntime.build_key(
            self._agent_manager.key_fields,
            user_id=user_id,
            agent_type=agent_type,
            key_values=key_values or None,
        )
        runtime = await self._agent_manager.pop_if_idle(key, idle_timeout_seconds)
        if runtime is None:
            return False
        agent_info = runtime.info
        logger.info(
            "[AgentOSRouter] reclaiming idle agent: user=%s agent_type=%s "
            "sandbox_id=%s idle_timeout=%.0fs",
            agent_info.user_id,
            agent_info.agent_type,
            agent_info.sandbox_id,
            idle_timeout_seconds,
        )
        await self._release_agent_resources(agent_info, best_effort=True)
        return True

    async def _release_agent_resources(
        self,
        agent_info: AgentInfo,
        *,
        best_effort: bool = False,
    ) -> None:
        """Delete YuanRong sandbox and unregister the registry instance."""
        await self._close_ws_client(agent_info.sandbox_id)
        if agent_info.sandbox_id:
            try:
                await self._yuanrong.delete_sandbox(agent_info.sandbox_id)
            except Exception:
                logger.exception(
                    "[AgentOSRouter] delete sandbox failed: sandbox_id=%s",
                    agent_info.sandbox_id,
                )
                if not best_effort:
                    raise
        try:
            await self._unregister_agent(agent_info)
        except Exception:
            logger.exception(
                "[AgentOSRouter] unregister agent failed: agent_id=%s",
                agent_info.agent_id,
            )
            if not best_effort:
                raise

    async def _unregister_agent(self, agent_info: AgentInfo) -> None:
        await self._registry.unregister_agent(
            agent_info.agent_id,
            user_id=agent_info.user_id,
            agent_type=agent_info.agent_type,
        )

    async def _register_agent(self, agent_info: AgentInfo) -> None:
        try:
            await self._registry.register_agent(agent_info)
        except Exception:
            logger.exception(
                "[AgentOSRouter] async registry registration failed: agent_id=%s",
                agent_info.agent_id,
            )
            return

        # 创建后查询 YuanRong 获取 node_ip / sandbox_ip，更新注册中心 instance
        # 的 placement 字段（node + address），供调度/路由使用。
        try:
            instance_info = await self._yuanrong.get_agent_info(
                agent_info.sandbox_id
            )
            node_ip = str(instance_info.get("node_ip") or "").strip()
            sandbox_ip = str(instance_info.get("sandbox_ip") or "").strip()
            if node_ip or sandbox_ip:
                service_id = instance_service_id(
                    agent_info.user_id, agent_info.agent_type
                )
                await self._registry.update_instance(
                    service_id,
                    node=node_ip or None,
                    address=sandbox_ip or None,
                )
                logger.info(
                    "[AgentOSRouter] registry instance updated: "
                    "service_id=%s node=%s address=%s",
                    service_id,
                    node_ip,
                    sandbox_ip,
                )
        except Exception:
            logger.exception(
                "[AgentOSRouter] registry instance update failed: agent_id=%s",
                agent_info.agent_id,
            )

    @staticmethod
    def _extract_user_id(envelope: E2AEnvelope) -> str:
        user_id = str(envelope.user_id or "").strip()
        if not user_id:
            raise ValueError("user_id is required for AgentOS routing")
        return user_id

    @staticmethod
    def _extract_agent_type(envelope: E2AEnvelope) -> str:
        raw = envelope.params.get("agent_type")
        if raw is None:
            raw = envelope.channel_context.get("agent_type")
        try:
            return AgentRuntime.normalize_agent_type(raw)
        except ValueError as exc:
            raise UnsupportedAgentType(str(exc)) from exc

    @staticmethod
    def _routing_error_response(
        envelope: E2AEnvelope,
        message: str,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            ok=False,
            payload={"error": message},
        )

    @staticmethod
    def _routing_error_chunk(
        envelope: E2AEnvelope,
        message: str,
    ) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            payload={"error": message},
            is_complete=True,
        )
