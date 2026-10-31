# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 AgentServerClient：route → HTTP 发信封 → touch。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.request_identity import web_routing_identity
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.extensions.runtime_management_extension.agent_authorization import (
    authorize_agent,
)
from jiuwenswarm.extensions.runtime_management_extension.invoke_ids import (
    apply_invoke_ids_to_envelope,
)
from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import (
    HttpSseAgentServerClient,
)
from jiuwenswarm.extensions.runtime_management_extension.session_route_client import (
    FatalRouteError,
    RetryableRouteError,
    RuntimeSessionRouteClient,
)
from gateway.routing.agent_client import AgentServerClient

logger = logging.getLogger(__name__)

_ROUTE_ATTEMPTS = 3
_TOUCH_INTERVAL_SECONDS = 20.0
_INTERRUPT_METHODS = frozenset(
    {
        "chat.interrupt",
        "chat.cancel",
    }
)
_ROUTELESS_METHODS = frozenset(
    {
        "hub.equipment.catalog",
        "agent.reload_config",
        "agent.prewarm.sync",
        "agent.prewarm_sync",
        "browser.runtime_restart",
        "initialize",
    }
)


def http_base_from_pod_sse_url(pod_sse_url: str) -> str:
    """``http://ip:8080/sse`` → ``http://ip:8080``，交给协议层再拼 ``/api/v1``。"""
    parsed = urlsplit((pod_sse_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FatalRouteError(
            f"invalid pod_sse_url: {pod_sse_url!r}",
            code="VALIDATION",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            value = value[0]
        text = str(value).strip()
        if text:
            return text
    return ""


def _from_maps(key: str, *maps: Any) -> str:
    for mapping in maps:
        if not isinstance(mapping, dict):
            continue
        found = _first_text(mapping.get(key))
        if found:
            return found
        nested = mapping.get("query")
        if isinstance(nested, dict):
            found = _first_text(nested.get(key))
            if found:
                return found
        ext = mapping.get("ext")
        if isinstance(ext, dict):
            found = _first_text(ext.get(key))
            if found:
                return found
    return ""


def _is_heartbeat_envelope(envelope: E2AEnvelope) -> bool:
    if str(envelope.request_id or "").startswith("heartbeat-"):
        return True
    params = envelope.params if isinstance(envelope.params, dict) else {}
    run = params.get("run")
    if isinstance(run, dict) and str(run.get("kind") or "").strip().lower() == "heartbeat":
        return True
    return False


def _is_routeless_envelope(envelope: E2AEnvelope) -> bool:
    """无需 session route 的管理类请求（如启动期 reload_config）。"""
    if _is_heartbeat_envelope(envelope):
        return True
    method = str(envelope.method or "").strip().lower()
    return method in _ROUTELESS_METHODS


def _is_interrupt_envelope(envelope: E2AEnvelope) -> bool:
    """用户停止 / cancel：需打到真正跑任务的 AgentServer，必要时多播。"""
    method = str(envelope.method or "").strip().lower()
    return method in _INTERRUPT_METHODS


def _rebind_timeout() -> float:
    try:
        value = float(os.getenv("GATEWAY_RUNTIME_SESSION_REBIND_TIMEOUT", "2"))
    except (TypeError, ValueError):
        return 2.0
    return value if value > 0 else 2.0


def _default_http_base() -> str:
    """管理类请求（reload_config 等）的默认 Agent HTTP Base。

    须指向 Agent ``http_server``（默认 ``AGENT_HTTP_PORT=8766``），
    **不要**打 WS 口 ``AGENT_SERVER_PORT=18092``（对该口发 HTTP 会 426/400 且无 JSON）。
    """
    explicit = os.getenv("GATEWAY_RUNTIME_DEFAULT_HTTP_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("AGENT_HTTP_PORT", "8766").strip() or "8766"
    return f"http://{host}:{port}"


def identity_from_envelope(
    envelope: E2AEnvelope,
    *,
    authorized_identity: tuple[str, str, str] | None = None,
) -> tuple[str, str, str, str, str | None]:
    """返回 ``session_id, group_id, bot_id, request_id, user_id``。"""
    params = envelope.params if isinstance(envelope.params, dict) else {}
    ctx = envelope.channel_context if isinstance(envelope.channel_context, dict) else {}
    # Web 进站把 group_id/bot_id 放在 channel_context.routing，不摊到顶层。
    # session.create 不走聊天授权，这里必须自己读 routing，否则 group 会落到
    # default，企业版按组织准入的 scope 匹配不上，创建对话直接失败。
    routed = web_routing_identity(ctx)
    request_id = _first_text(envelope.request_id)
    user_id = _first_text(
        envelope.user_id,
        params.get("user_id"),
        _from_maps("user_id", params, ctx),
        routed.get("user_id"),
    ) or None
    group_id = (
        _from_maps("group_id", params, ctx)
        or routed.get("group_id")
        or _first_text(envelope.chat_id)
    )
    bot_id = _from_maps("bot_id", params, ctx) or routed.get("bot_id")
    if not bot_id:
        bot_id = _from_maps("app_id", params, ctx)
    if not bot_id and isinstance(envelope.agent_ref, dict):
        bot_id = _first_text(
            envelope.agent_ref.get("bot_id"),
            envelope.agent_ref.get("agent_id"),
            envelope.agent_ref.get("id"),
        )
    if not bot_id:
        bot_id = _first_text(envelope.agent_id)
    # 如果 envelope 没带 group_id/bot_id/user_id，用默认值 route 到 wildcard scope
    if not user_id:
        user_id = "default"
    if not group_id:
        group_id = "default"
    if not bot_id:
        bot_id = "default"
    # 授权身份必须在合成 session key 前生效，不能仅覆盖最终路由参数。
    if authorized_identity is not None:
        group_id, bot_id, user_id = authorized_identity
    session_id = _first_text(envelope.session_id, params.get("session_id"))
    if not session_id and group_id and bot_id:
        session_id = f"{group_id}:{bot_id}:{user_id or '_'}"
    if not (session_id and group_id and bot_id and request_id):
        raise FatalRouteError(
            "route requires session_id/group_id/bot_id/request_id on envelope",
            code="VALIDATION",
        )
    return session_id, group_id, bot_id, request_id, user_id


def _is_http_retryable(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (httpx.RequestError, httpx.HTTPStatusError, OSError, TimeoutError, asyncio.TimeoutError),
    )


class RuntimeRoutedAgentClient(AgentServerClient):
    """查 Runtime Manager 路由，再用 HTTP 客户端把信封发到对应 Pod。"""

    def __init__(
        self,
        *,
        route_client: RuntimeSessionRouteClient | None = None,
        http_client: HttpSseAgentServerClient | None = None,
        touch_interval_seconds: float = _TOUCH_INTERVAL_SECONDS,
        route_attempts: int = _ROUTE_ATTEMPTS,
    ) -> None:
        self._route = route_client or RuntimeSessionRouteClient()
        self._owns_route = route_client is None
        self._http = http_client or HttpSseAgentServerClient()
        self._owns_http = http_client is None
        self._touch_interval_seconds = max(0.0, float(touch_interval_seconds))
        self._route_attempts = max(1, int(route_attempts))
        self._connected = False
        self._on_server_push: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._pod_clients: dict[str, HttpSseAgentServerClient] = {}
        # [PERF] 全局连接锁改 per-pod:10 个冷 Pod 首波建连由串行 ~0.3s×N 变并行 ~0.3s
        self._pod_connect_locks: dict[str, asyncio.Lock] = {}
        self._pod_locks_guard = asyncio.Lock()
        self._rpc_origins: dict[tuple[str, str], str] = {}
        self._drop_tasks: set[asyncio.Task[None]] = set()
        self._last_rebind_warn = 0.0   # rebind 降级告警节流（滚动期旧 runtime 404 防刷屏）
        # session_id → 当前仍在推流的 AgentServer base_url。长工具无 chunk 时
        # Runtime 亲和可能过期；interrupt 多播时带上这里，避免杀错 Pod。
        self._session_stream_bases: dict[str, str] = {}

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        self._on_server_push = handler

    async def _handle_pod_push(self, base_url: str, wire: dict[str, Any]) -> None:
        from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk

        handler = self._on_server_push
        if handler is None:
            return
        chunk = parse_agent_server_wire_chunk(wire)
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        rpc = payload.get("jsonrpc", payload)
        key = None
        if isinstance(rpc, dict) and rpc.get("id") is not None and rpc.get("method"):
            key = (str(wire.get("session_id") or ""), str(rpc["id"]))
            self._rpc_origins[key] = base_url
        try:
            await handler(wire)
        except BaseException:
            if key is not None:
                self._rpc_origins.pop(key, None)
            raise

    async def _pod_lock_for(self, base_url: str) -> asyncio.Lock:
        """[PERF] 按 Pod 取连接锁:不同 Pod 的建连互不阻塞。"""
        async with self._pod_locks_guard:
            lock = self._pod_connect_locks.get(base_url)
            if lock is None:
                lock = self._pod_connect_locks[base_url] = asyncio.Lock()
            return lock

    async def _ensure_pod_push(self, base_url: str) -> None:
        if self._on_server_push is None:
            return
        stale: HttpSseAgentServerClient | None = None
        async with await self._pod_lock_for(base_url):
            self._ensure_connected()
            client = self._pod_clients.get(base_url)
            # Fallback if push-done drop hasn't run yet (same IP reused).
            if client is not None and not getattr(client, "_running", True):
                stale = self._pod_clients.pop(base_url)
                client = None
            if client is None:
                client = HttpSseAgentServerClient(retry_push_connect=False)
                client.set_server_push_handler(
                    lambda wire: self._handle_pod_push(base_url, wire)
                )
                try:
                    await client.connect(base_url)
                    await client.wait_push_ready()
                except BaseException:
                    await client.disconnect()
                    raise
                self._pod_clients[base_url] = client
                client.add_push_done_callback(
                    lambda _task, url=base_url, bound=client: self._schedule_drop_pod_client(
                        url, bound
                    )
                )
        if stale is not None:
            try:
                await stale.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RuntimeRouted] 断开旧 Pod 推送客户端失败: %s (%s)",
                    base_url,
                    exc,
                )
        await client.wait_push_ready()

    def _schedule_drop_pod_client(
        self, base_url: str, client: HttpSseAgentServerClient
    ) -> None:
        if not self._connected:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._drop_pod_client(base_url, client),
                name="runtime-drop-pod-push",
            )
        except RuntimeError:
            return
        self._drop_tasks.add(task)
        task.add_done_callback(self._drop_tasks.discard)

    async def _drop_pod_client(
        self, base_url: str, client: HttpSseAgentServerClient
    ) -> None:
        async with await self._pod_lock_for(base_url):
            if self._pod_clients.get(base_url) is not client:
                return
            self._pod_clients.pop(base_url, None)
        logger.warning(
            "[RuntimeRouted] 推送循环已结束，丢弃客户端: %s",
            base_url,
        )
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RuntimeRouted] 断开旧 Pod 推送客户端失败: %s (%s)",
                base_url,
                exc,
            )


    async def connect(self, uri: str) -> None:
        _ = uri
        self._connected = True

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        """develop 的客户端接口要求实现这个方法。runtime 客户端不缓存这份配置。"""
        _ = config, env

    @property
    def server_ready(self) -> bool:
        """WebChannel on_connect 依赖此标志发送 connection.ack。"""
        return self._connected

    async def disconnect(self) -> None:
        self._connected = False
        async with self._pod_locks_guard:
            clients = list(self._pod_clients.values())
            self._pod_clients.clear()
            self._pod_connect_locks.clear()
        drop_tasks = list(self._drop_tasks)
        self._drop_tasks.clear()
        if drop_tasks:
            await asyncio.gather(*drop_tasks, return_exceptions=True)
        results = await asyncio.gather(
            *(client.disconnect() for client in clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Failed to disconnect routed push client: %s", result)
        self._rpc_origins.clear()
        if self._owns_http:
            try:
                await self._http.disconnect()
            except Exception as exc:
                logger.warning("Failed to disconnect HTTP client: %s", exc)
        if self._owns_route:
            try:
                await self._route.aclose()
            except Exception as exc:
                logger.warning("Failed to close route client: %s", exc)

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("client not connected")

    def _heartbeat_response(self, envelope: E2AEnvelope) -> AgentResponse:
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or "web"),
            ok=True,
            payload={},
        )

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        self._ensure_connected()
        authorized_identity = await authorize_agent(envelope)
        if envelope.method == "acp.tool_response":
            params = envelope.params or {}
            key = (
                str(envelope.session_id or params.get("session_id") or ""),
                str(params.get("jsonrpc_id") or ""),
            )
            base_url = self._rpc_origins.pop(key, None)
            if base_url is None:
                raise RuntimeError("Reverse RPC origin Pod is unavailable")
            return await self._http.send_request(envelope, base_url=base_url)
        if _is_heartbeat_envelope(envelope):
            return self._heartbeat_response(envelope)
        # 对齐旧 RuntimeManagement：发 Agent 前补齐并 MD5 service/agent/workspace。
        apply_invoke_ids_to_envelope(envelope)
        if _is_routeless_envelope(envelope):
            result = await self._http.send_request(envelope, base_url=_default_http_base())
            return result
        session_id, group_id, bot_id, request_id, user_id = identity_from_envelope(
            envelope, authorized_identity=authorized_identity,
        )
        if _is_interrupt_envelope(envelope):
            return await self._send_interrupt_fanout(
                envelope,
                session_id=session_id,
                group_id=group_id,
                bot_id=bot_id,
                request_id=request_id,
                user_id=user_id,
            )
        base_url, route_id = await self._route_with_retry(
            session_id=session_id,
            group_id=group_id,
            bot_id=bot_id,
            request_id=request_id,
            user_id=user_id,
        )
        try:
            await self._ensure_pod_push(base_url)
            result = await self._http.send_request(envelope, base_url=base_url)
        except Exception as exc:
            if not _is_http_retryable(exc):
                raise
            logger.warning(
                "[RuntimeRouted] unary http failed, re-route: session=%s err=%s",
                session_id,
                exc,
            )
            base_url, route_id = await self._route_with_retry(
                session_id=session_id,
                group_id=group_id,
                bot_id=bot_id,
                request_id=uuid.uuid4().hex,
                user_id=user_id,
            )
            await self._ensure_pod_push(base_url)
            result = await self._http.send_request(envelope, base_url=base_url)
        await self._touch_quiet(session_id, route_id)
        await self._rebind_after_create(envelope, result, session_id)
        return result

    async def _rebind_after_create(
        self, envelope: E2AEnvelope, result: AgentResponse, session_id: str,
    ) -> None:
        """session.create 响应返回上层之前，把临时路由 key 原子改绑为真实 id。

        修复（runtime 侧 2026-09-session-create-rebind）：create 以临时 key
        （sess_*/webhttp_*/身份组合串）route 占槽，真实 id 首条 chat.send 会二次
        占槽（双计数）且可能落别的 Pod（亲和断裂）。本方法在 ``send_request``
        返回前 await——**时序保证**：客户端拿到真实 id 时改绑已完成，chat.send
        不可能抢先。

        - 响应 ok：rebind(临时 key → 真实 id)，亲和钉在 create 实际服务的 Pod；
        - 响应失败：rebind(临时 key → 空) 驱逐，立即释放槽位（不等 session_ttl）；
        - 失败/超时一律降级（warning 节流 + 照常返回响应）＝旧行为；旧 runtime
          （无 /api/session/rebind 端点）同走降级，滚动期不阻塞 create。
        """
        if str(envelope.method or "") != "session.create":
            return
        payload = result.payload if isinstance(result.payload, dict) else {}
        if result.ok:
            real = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
        else:
            real = ""
        if real and real == session_id:
            return
        try:
            action = await asyncio.wait_for(
                self._route.rebind(
                    from_session_id=session_id,
                    to_session_id=real or None,
                    request_id=uuid.uuid4().hex,
                ),
                timeout=_rebind_timeout(),
            )
            logger.info(
                "[RuntimeRouted] session.create rebind: from=%s to=%s action=%s",
                session_id,
                real or "(evict)",
                action,
            )
        except Exception as exc:  # noqa: BLE001 - 降级=现状（临时 key 等 TTL 过期）
            now = time.monotonic()
            if now - self._last_rebind_warn >= 60.0:
                self._last_rebind_warn = now
                logger.warning(
                    "[RuntimeRouted] session.create rebind failed (degraded, "
                    "temp key expires via session_ttl): %s",
                    exc,
                )
            else:
                logger.debug(
                    "[RuntimeRouted] session.create rebind failed: %s", exc,
                )

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        self._ensure_connected()
        authorized_identity = await authorize_agent(envelope)
        if _is_heartbeat_envelope(envelope):
            yield AgentResponseChunk(
                request_id=str(envelope.request_id or ""),
                channel_id=str(envelope.channel or "web"),
                payload={},
                is_complete=True,
            )
            return
        # 对齐旧 RuntimeManagement：发 Agent 前补齐并 MD5 service/agent/workspace。
        apply_invoke_ids_to_envelope(envelope)
        if _is_routeless_envelope(envelope):
            async for chunk in self._http.send_request_stream(
                envelope, base_url=_default_http_base()
            ):
                yield chunk
            return
        session_id, group_id, bot_id, request_id, user_id = identity_from_envelope(
            envelope, authorized_identity=authorized_identity,
        )
        base_url, route_id = await self._route_with_retry(
            session_id=session_id,
            group_id=group_id,
            bot_id=bot_id,
            request_id=request_id,
            user_id=user_id,
        )
        yielded = 0
        try:
            async for chunk in self._stream_with_touch(
                envelope,
                base_url=base_url,
                session_id=session_id,
                route_id=route_id,
                group_id=group_id,
                bot_id=bot_id,
                user_id=user_id,
            ):
                yielded += 1
                yield chunk
        except Exception as exc:
            if yielded or not _is_http_retryable(exc):
                raise
            logger.warning(
                "[RuntimeRouted] stream http failed before first chunk, re-route: session=%s err=%s",
                session_id,
                exc,
            )
            base_url, route_id = await self._route_with_retry(
                session_id=session_id,
                group_id=group_id,
                bot_id=bot_id,
                request_id=uuid.uuid4().hex,
                user_id=user_id,
            )
            async for chunk in self._stream_with_touch(
                envelope,
                base_url=base_url,
                session_id=session_id,
                route_id=route_id,
                group_id=group_id,
                bot_id=bot_id,
                user_id=user_id,
            ):
                yield chunk
        finally:
            await self._touch_quiet(session_id, route_id)

    async def _stream_with_touch(
        self,
        envelope: E2AEnvelope,
        *,
        base_url: str,
        session_id: str,
        route_id: str,
        group_id: str,
        bot_id: str,
        user_id: str | None,
    ) -> AsyncIterator[AgentResponseChunk]:
        """推流并独立保活 Runtime 亲和。

        长工具（如 ``uv pip``）可能数分钟无 stream chunk；若只在收到 chunk 时
        touch，亲和会静默过期，随后 ``chat.interrupt`` 会 route 到别的 Pod，
        杀不到真正在跑的 bash。后台任务按 ``_touch_interval_seconds`` 周期 touch。
        """
        self._session_stream_bases[session_id] = base_url
        # 可变槽：保活任务在亲和恢复后更新 route_id
        touch_state: dict[str, str] = {"route_id": route_id}
        keepalive_task: asyncio.Task[None] | None = None

        async def _keepalive_loop() -> None:
            interval = self._touch_interval_seconds
            if interval <= 0:
                return
            consecutive_fail = 0
            while True:
                await asyncio.sleep(interval)
                touched = await self._touch_quiet(session_id, touch_state["route_id"])
                if touched is True:
                    consecutive_fail = 0
                    continue
                consecutive_fail += 1
                logger.warning(
                    "[RuntimeRouted] stream keepalive touch miss: session=%s "
                    "base=%s touched=%s fails=%s",
                    session_id,
                    base_url,
                    touched,
                    consecutive_fail,
                )
                new_route_id = await self._restore_stream_affinity(
                    session_id=session_id,
                    group_id=group_id,
                    bot_id=bot_id,
                    user_id=user_id,
                    stream_base_url=base_url,
                )
                if new_route_id:
                    touch_state["route_id"] = new_route_id
                    consecutive_fail = 0

        await self._ensure_pod_push(base_url)
        if self._touch_interval_seconds > 0:
            keepalive_task = asyncio.create_task(
                _keepalive_loop(),
                name=f"runtime-touch:{session_id[:24]}",
            )
        last_touch = time.monotonic()
        try:
            async for chunk in self._http.send_request_stream(
                envelope, base_url=base_url
            ):
                # 保留 chunk 顺带 touch：有输出时也能刷新，且覆盖 interval=0 关闭后台的情况
                now = time.monotonic()
                if (
                    self._touch_interval_seconds > 0
                    and now - last_touch >= self._touch_interval_seconds
                ):
                    touched = await self._touch_quiet(
                        session_id, touch_state["route_id"]
                    )
                    last_touch = now
                    if touched is False:
                        new_route_id = await self._restore_stream_affinity(
                            session_id=session_id,
                            group_id=group_id,
                            bot_id=bot_id,
                            user_id=user_id,
                            stream_base_url=base_url,
                        )
                        if new_route_id:
                            touch_state["route_id"] = new_route_id
                yield chunk
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - 保活收尾不阻断推流清理
                    logger.debug(
                        "[RuntimeRouted] keepalive task cleanup error",
                        exc_info=True,
                    )
            if self._session_stream_bases.get(session_id) == base_url:
                self._session_stream_bases.pop(session_id, None)

    async def _restore_stream_affinity(
        self,
        *,
        session_id: str,
        group_id: str,
        bot_id: str,
        user_id: str | None,
        stream_base_url: str,
    ) -> str | None:
        """touch 失效后重新 route，尽量让亲和表重新指向当前仍在推流的 Pod。

        Runtime 无「钉死指定 Pod」API 时，新 route 可能漂到别的 Pod；此时仍记录
        警告，并依赖 ``_session_stream_bases`` + interrupt 多播杀到真正跑任务的机器。
        返回可用于后续 touch 的 ``route_id``；失败返回 ``None``。
        """
        try:
            new_base, new_route_id = await self._route_with_retry(
                session_id=session_id,
                group_id=group_id,
                bot_id=bot_id,
                request_id=uuid.uuid4().hex,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001 - 亲和恢复失败不打断推流
            logger.warning(
                "[RuntimeRouted] affinity restore failed: session=%s stream_base=%s err=%s",
                session_id,
                stream_base_url,
                exc,
            )
            return None
        if new_base != stream_base_url:
            logger.warning(
                "[RuntimeRouted] affinity restore drifted: session=%s "
                "stream_base=%s routed_base=%s — interrupt will fan out",
                session_id,
                stream_base_url,
                new_base,
            )
        else:
            logger.info(
                "[RuntimeRouted] affinity restored to stream pod: session=%s base=%s",
                session_id,
                stream_base_url,
            )
        # 推流仍钉在原 Pod；表项供 interrupt 优先打到真正执行方
        self._session_stream_bases[session_id] = stream_base_url
        return new_route_id

    async def _interrupt_target_bases(self, session_id: str, routed_base: str) -> list[str]:
        """interrupt 目标顺序：推流中的 Pod → route 结果 → 本 GW 已连接的其它 Pod。"""
        ordered: list[str] = []
        stream_base = self._session_stream_bases.get(session_id, "")
        for candidate in (stream_base, routed_base):
            text = (candidate or "").strip()
            if text and text not in ordered:
                ordered.append(text)
        async with self._pod_locks_guard:
            pooled = list(self._pod_clients.keys())
        for base in pooled:
            if base and base not in ordered:
                ordered.append(base)
        return ordered

    async def _send_interrupt_fanout(
        self,
        envelope: E2AEnvelope,
        *,
        session_id: str,
        group_id: str,
        bot_id: str,
        request_id: str,
        user_id: str | None,
    ) -> AgentResponse:
        """先按 session route 打一枪，再对其它已连接 AgentServer 多播（幂等 no-op）。"""
        base_url, route_id = await self._route_with_retry(
            session_id=session_id,
            group_id=group_id,
            bot_id=bot_id,
            request_id=request_id,
            user_id=user_id,
        )
        targets = await self._interrupt_target_bases(session_id, base_url)
        logger.info(
            "[RuntimeRouted] interrupt fanout: session=%s targets=%s stream_base=%s",
            session_id,
            targets,
            self._session_stream_bases.get(session_id),
        )
        primary: AgentResponse | None = None
        last_error: Exception | None = None
        for target in targets:
            try:
                await self._ensure_pod_push(target)
                result = await self._http.send_request(envelope, base_url=target)
            except Exception as exc:  # noqa: BLE001 - 单 Pod 失败继续打其它
                last_error = exc
                logger.warning(
                    "[RuntimeRouted] interrupt to %s failed: session=%s err=%s",
                    target,
                    session_id,
                    exc,
                )
                continue
            payload = result.payload if isinstance(result.payload, dict) else {}
            logger.info(
                "[RuntimeRouted] interrupt to %s: session=%s ok=%s killed=%s "
                "event=%s",
                target,
                session_id,
                result.ok,
                payload.get("killed"),
                payload.get("event_type"),
            )
            if primary is None:
                primary = result
            # 已有明确杀进程计数则不必再扫（其余 Pod 无会话会 no-op，但可省 RTT）
            killed = payload.get("killed")
            if isinstance(killed, int) and killed > 0:
                break
            cancelled_tools = payload.get("cancelled_tools")
            if cancelled_tools:
                break
        await self._touch_quiet(session_id, route_id)
        if primary is not None:
            return primary
        if last_error is not None:
            raise last_error
        return AgentResponse(
            request_id=str(envelope.request_id or request_id),
            channel_id=str(envelope.channel or "web"),
            ok=True,
            payload={"event_type": "chat.interrupt_result", "success": True},
        )

    async def _route_with_retry(
        self,
        *,
        session_id: str,
        group_id: str,
        bot_id: str,
        request_id: str,
        user_id: str | None,
    ) -> tuple[str, str]:
        last_error: RetryableRouteError | None = None
        route_id = request_id
        for attempt in range(1, self._route_attempts + 1):
            try:
                result = await self._route.route(
                    session_id=session_id,
                    group_id=group_id,
                    bot_id=bot_id,
                    request_id=route_id,
                    user_id=user_id,
                )
                return http_base_from_pod_sse_url(result.pod_sse_url), result.request_id
            except RetryableRouteError as exc:
                last_error = exc
                delay = float(exc.retry_after) if exc.retry_after is not None else 1.0
                logger.warning(
                    "[RuntimeRouted] route retryable: code=%s attempt=%s/%s sleep=%.1fs",
                    exc.code,
                    attempt,
                    self._route_attempts,
                    delay,
                )
                if attempt >= self._route_attempts:
                    break
                await asyncio.sleep(delay)
        if last_error is None:
            raise RetryableRouteError("route failed", code="TRANSPORT")
        raise last_error

    async def _touch_quiet(self, session_id: str, request_id: str) -> bool | None:
        try:
            return await self._route.touch(session_id=session_id, request_id=request_id)
        except Exception:
            logger.warning(
                "[RuntimeRouted] touch failed: session=%s request_id=%s",
                session_id,
                request_id,
                exc_info=True,
            )
            return None
