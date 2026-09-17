# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Standalone Gateway entrypoint (split deployment).

This process starts:
- Gateway MessageHandler + ChannelManager
- WebChannel websocket server (browser inbound)
- Heartbeat service
- Cron scheduler service (triggers remote AgentServer via ws)

It connects to a remote/local AgentServer WebSocket endpoint.

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import sys
import time
import uuid as uuid_module
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from openjiuwen.core.common.logging import LogManager
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from jiuwenswarm.common.ws_diagnostics import format_ws_diagnostics, describe_ws_peer, describe_ws_exception

# user_id 白名单: 仅允许字母数字及 _-, 拒绝路径遍历字符
_SAFE_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
from jiuwenswarm.gateway.channel_manager.base import BaseWebChannel

parse_dotenv_early("jiuwenswarm-gateway")

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect import AcpGatewayBridge
from jiuwenswarm.gateway.routing.agent_request_timeout import coerce_client_timeout_ms
from jiuwenswarm.common.security.ws_origin import get_header_value
from jiuwenswarm.gateway.routing.route_binding import GatewayRouteBinding
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.utils import (
    ensure_default_builtin_skills,
    get_cron_jobs_path,
    get_env_file,
    get_root_dir,
    get_user_workspace_dir,
    prepare_workspace,
    reset_free_search_runtime_flags,
)
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod, Message, Mode

# Ensure workspace initialized
_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
_new_workspace = _workspace_dir / "agent" / "workspace"
_old_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"

# Initialize if config doesn't exist, or if legacy workspace exists but new doesn't (migration),
# or if the preset MCP package dir isn't seated yet (an install predating the
# mcp_builtins zip-seed feature would otherwise skip an already-initialized
# workspace, leaving mcp_builtins absent and mcp.list empty).
_mcp_builtins_dir = _new_workspace / "mcp" / "mcp_builtins"
config_missing = not _config_file.exists()
workspace_migration_needed = _old_workspace.exists() and not _new_workspace.exists()
mcp_builtins_missing = not _mcp_builtins_dir.is_dir()

if config_missing or workspace_migration_needed or mcp_builtins_missing:
    prepare_workspace(overwrite=False)

# 幂等地补齐默认内置技能（对已有工作区也生效，新增默认技能时自动安装）
ensure_default_builtin_skills()

_logging_yaml = get_root_dir() / "config" / "logging.yaml"
if _logging_yaml.exists():
    from openjiuwen.core.common.logging.log_config import configure_log

    configure_log(str(_logging_yaml))
else:
    # Reduce openjiuwen internal logs (keep Gateway logs)
    for _lg in LogManager.get_all_loggers().values():
        _lg.set_level(logging.CRITICAL)

load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
reset_free_search_runtime_flags()

logger = logging.getLogger("jiuwenswarm.gateway")

# Keep gateway idle-finalize fallback aligned with ACP channel default.
_PROMPT_IDLE_FINALIZE_SECONDS = 3.0
_AGENT_PREWARM_EXCLUDED_CHANNELS = frozenset({"acp", "a2a"})

# IM 平台官方 API 域名（仅作为 config.yaml 缺字段时的加载兜底，不在 Config 类里硬编码）
_FEISHU_DEFAULT_API_BASE = "https://open.feishu.cn"
_DINGTALK_DEFAULT_API_BASE = "https://api.dingtalk.com"    # 新版 v1.0 接口域名
_DINGTALK_DEFAULT_OAPI_BASE = "https://oapi.dingtalk.com"  # 旧版 media 接口域名
_XIAOYI_DEFAULT_PUSH_URL = "https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook"


def _build_event_frame(msg) -> dict[str, Any]:
    event_name = "chat.final"
    if msg.event_type is not None:
        event_name = msg.event_type.value
    if isinstance(msg.payload, dict):
        payload = {**msg.payload}
        payload.setdefault("session_id", msg.session_id)
    else:
        payload = {"session_id": msg.session_id, "content": str(msg.payload or "")}
    return {"type": "event", "event": event_name, "payload": payload}


def _normalize_gateway_message(msg):

    req_method = getattr(msg, "req_method", None) or ReqMethod.CHAT_SEND
    params = dict(msg.params or {})
    if "query" not in params and "content" in params:
        params["query"] = params["content"]
    if req_method == ReqMethod.CHAT_RESUME:
        req_method = ReqMethod.CHAT_CANCEL
        params.setdefault("intent", "resume")

    method_val = req_method.value
    is_stream = bool(
        msg.is_stream
        or method_val in (ReqMethod.CHAT_SEND.value, ReqMethod.HISTORY_GET.value)
    )

    return Message(
        id=msg.id,
        type=msg.type,
        channel_id=msg.channel_id,
        session_id=msg.session_id,
        params=params,
        timestamp=msg.timestamp,
        ok=msg.ok,
        req_method=req_method,
        mode=msg.mode,
        is_stream=is_stream,
        stream_seq=msg.stream_seq,
        stream_id=msg.stream_id,
        metadata=msg.metadata,
        user_id=getattr(msg, "user_id", None),
    )


async def _normalize_and_forward_message(msg, channel_manager) -> bool:
    normalized = _normalize_gateway_message(msg)
    # ACP/直连转发路径(session.create 等)也需注入 work_mode 归一化,
    # 与 _norm_and_forward(Web/TUI 主路径)保持一致。否则直连 AgentServer 的
    # 调用方传真实 project_id + 合法但不匹配的 work_mode 时,AgentServer 侧
    # _work_mode_explicit marker 缺失,不会返回设计要求的 BAD_REQUEST。
    method_val = getattr(getattr(msg, "req_method", None), "value", None) or ""
    if method_val == "session.create":
        _inject_session_work_mode(normalized)
    await channel_manager.deliver_to_message_handler(normalized)
    logger.info("[App] Gateway inbound -> MessageHandler: id=%s channel_id=%s", msg.id, msg.channel_id)
    return False


def _inject_session_work_mode(msg: Message) -> None:
    """为 ``session.create`` 请求注入 work_mode 归一化(主路径兜底)。

    与 fallback ``_session_create`` 共用同一 helper
    ``resolve_session_work_mode_params``,保持主路径/fallback 一致。

    成功时写回归一化后的 ``project_id`` / ``project_dir`` / ``work_mode`` 到
    ``msg.params``;失败时(非法 work_mode)不写回,保留原始 params 由后续
    AgentServer 或 fallback ``_session_create`` 返回 BAD_REQUEST。

    本函数做参数归一化 + **TUI project_dir 预解析**(设计文档 §5.3.5):
    - TUI 通道下,若 project_id 为默认项目且 project_dir 非空绝对路径,
      按 work_mode="code" 查找/创建 code 项目,将真实 project_id 写回 params。
    - 非 TUI 通道仅做纯参数归一化,最终 work_mode 以 Project 记录为准的
      校验由 ``_session_create`` / AgentServer 侧完成。

    显式性判定(``has_explicit_work_mode``)由 ``resolve_session_work_mode_params``
    随 binding 返回。但 gateway → AgentServer 是跨进程通信,binding 结果不能
    直接传递。因此 gateway 将 ``has_explicit_work_mode`` 注入 params 的
    ``_work_mode_explicit`` 字段作为传输标记,AgentServer 消费后立即 pop。
    AgentServer 直连调用方(非 gateway 路径)不携带此标记,此时 AgentServer
    通过重新调用 ``resolve_session_work_mode_params`` 获取 binding 中的
    ``has_explicit_work_mode``(直连场景 params 为原始值,计算结果正确)。
    """
    params = getattr(msg, "params", None)
    if not isinstance(params, dict):
        return
    channel_id = getattr(msg, "channel_id", None)
    try:
        from jiuwenswarm.server.runtime.session.work_mode import resolve_session_work_mode_params
        binding = resolve_session_work_mode_params(params, channel_id=channel_id)
    except Exception:  # noqa: BLE001
        # 归一化异常时不写回,保留原始 params 由后续处理
        return
    if binding.error:
        return

    resolved_project_id = binding.project_id
    resolved_project_dir = binding.project_dir
    resolved_work_mode = binding.work_mode

    # 注意:TUI 通道的 session.create 不走 forward 路径(不在 CLI_FORWARD_REQ_METHODS),
    # TUI 预解析在 tui_connect.py 的 _session_create 中通过 find_or_create_code_project_for_tui_params 完成。
    # 此处仅处理 WEB/ACP 通道的 work_mode 绑定注入。

    params["project_id"] = resolved_project_id
    params["project_dir"] = resolved_project_dir
    params["work_mode"] = resolved_work_mode
    # _work_mode_explicit: 跨进程传输"请求是否显式传了 work_mode"标志。
    # AgentServer 消费后立即 pop,不持久化、不序列化到日志。
    # 该标志区分"用户显式传 work_mode"(需做 Project 一致性校验)与
    # "gateway 注入通道默认 work_mode"(跳过一致性校验,以 Project 记录为准)。
    params["_work_mode_explicit"] = binding.has_explicit_work_mode


class _InboundGatewayServer:
    """Gateway internal inbound service for forwarding channel messages to MessageHandler."""

    def __init__(self, inbound_handler):
        self._inbound_handler = inbound_handler
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._serve_loop(), name="gateway-inbound-server")

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def handle_message(self, msg) -> bool:
        await self._queue.put(msg)
        return True

    async def _serve_loop(self) -> None:
        while self._running:
            try:
                msg = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                handled = self._inbound_handler(msg)
                if asyncio.iscoroutine(handled):
                    await handled
            except Exception:  # noqa: BLE001
                logger.exception("[App] Gateway inbound handling failed: id=%s", getattr(msg, "id", None))


async def _connect_with_retry(
        client,
        uri: str,
        *,
        max_retries: int = 20,
        interval: float = 3.0,
) -> None:
    """连接 AgentServer, 失败重试.

    相比固定 3s 间隔: 先做轻量 TCP 端口探测, 端口通了再 ws 握手, 避免 AgentServer
    尚未 listen 时反复走完整 WS 握手; 重试间隔改为指数退避 (0.2s 起, 翻倍至上限
    ``interval``), 让 AgentServer 一旦 listen 即在亚秒级连上, 而非硬等下一个 3s 节拍.
    """
    import socket as _socket
    from urllib.parse import urlparse as _urlparse

    parsed = _urlparse(uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    def _tcp_ready(timeout: float = 0.5) -> bool:
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    backoff = 0.2
    for attempt in range(1, max_retries + 1):
        # 先 TCP 探测: 端口未通则不浪费一次 WS 握手, 直接进入退避等待.
        if not _tcp_ready():
            if attempt >= max_retries:
                logger.error(
                    "[App] connect AgentServer failed after %d tries: port %s not listening  uri=%s",
                    attempt, port, uri,
                )
                raise ConnectionRefusedError(f"AgentServer port {port} not listening")
            logger.info(
                "[App] AgentServer port %s not listening yet (%d/%d), retry in %.2fs...",
                port, attempt, max_retries, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, interval)
            continue
        try:
            await client.connect(uri)
            logger.info("[App] connected to AgentServer: %s", uri)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries:
                logger.error(
                    "[App] connect AgentServer failed after %d tries: %s  last=%s",
                    attempt,
                    uri,
                    exc,
                )
                raise
            logger.warning(
                "[App] connect AgentServer failed (%d/%d): %s  retry in %.2fs...",
                attempt,
                max_retries,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, interval)


def _exec_gateway_restart() -> None:
    logger.info("[App] .env updated, restarting Gateway...")
    os.execv(sys.executable, [sys.executable, *sys.argv])


@dataclass
class GatewayRestartRequest:
    requested: bool = False
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)


def _schedule_gateway_restart(
        restart_request: GatewayRestartRequest | None = None,
        *,
        delay: float = 2.0,
) -> None:
    if restart_request is not None:
        restart_request.requested = True

    def _request_restart() -> None:
        if restart_request is not None:
            restart_request.ready_event.set()
            return
        _exec_gateway_restart()

    try:
        loop = asyncio.get_running_loop()
        loop.call_later(delay, _request_restart)
    except RuntimeError:
        _request_restart()


async def _wait_for_gateway_tasks_or_restart(
        tasks_to_wait: list[asyncio.Task],
        restart_request: GatewayRestartRequest,
) -> bool:
    restart_task = asyncio.create_task(restart_request.ready_event.wait(), name="gateway-restart")
    service_tasks = set(tasks_to_wait)
    wait_tasks = set(service_tasks)
    wait_tasks.add(restart_task)
    try:
        while wait_tasks:
            done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if restart_task in done:
                return restart_request.requested or restart_request.ready_event.is_set()
            for task in done:
                wait_tasks.discard(task)
                service_tasks.discard(task)
                if task.cancelled():
                    raise asyncio.CancelledError
                exc = task.exception()
                if exc is not None:
                    if restart_request.requested:
                        logger.warning("[App] service task failed while Gateway restart is pending: %s", exc)
                        return True
                    raise exc
            if not service_tasks:
                return restart_request.requested or restart_request.ready_event.is_set()
        return restart_request.requested or restart_request.ready_event.is_set()
    finally:
        if not restart_task.done():
            restart_task.cancel()
            try:
                await restart_task
            except asyncio.CancelledError:
                pass


@dataclass
class RouteConfig:
    """单条路由的配置（/acp, /cli 等）。"""

    path: str
    channel_id: str
    forward_methods: frozenset[str] = frozenset()
    forward_no_local_handler_methods: frozenset[str] = frozenset()
    local_handlers: dict[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    inbound_interceptor: Callable[..., Awaitable[bool]] | None = None
    outbound_interceptor: Callable[..., Awaitable[bool]] | None = None
    cleanup_handler: Callable[..., Any] | None = None
    disconnect_handler: Callable[..., Any] | None = None
    session_bind_handler: Callable[..., Any] | None = None
    # V2: 委托 ws 注册的外部 Channel（如 tui 的 TuiChannel）。
    # GatewayServer 仍作 ws 宿主 + 入站帧解析，但把 ws + RoutingKey 委托注册进该
    # Channel 的五维索引（_clients_by_key / _ws_by_id），出站由 ChannelManager 派发
    # 到该 Channel.send（按 delivery.ws_id 物理寻址）。None 表示该 route 无需委托
    # （如 acp 自带 _session_to_client 反查，web 有独立 WebChannel 不经此路径）。
    ws_channel: Any = None


@dataclass
class GatewayServerConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 19001
    routes: dict[str, RouteConfig] = field(default_factory=dict)
    path: str | None = None
    channel_id: str | None = None

    def __post_init__(self) -> None:
        if self.routes:
            return
        path = str(self.path or "").strip()
        channel_id = str(self.channel_id or "").strip()
        if path and channel_id:
            self.routes[path] = RouteConfig(path=path, channel_id=channel_id)


@dataclass(frozen=True)
class _LocalHandlerContext:
    ws: Any
    req_id: str
    params: dict[str, Any]
    session_id: str
    user_id: str | None


class GatewayServer(BaseWebChannel):
    """通用多路路由 WebSocket Gateway Server。

    支持多个路径（如 /acp、/cli），每条路径可以有独立的 channel_id 和本地 handler。
    本地 handler 优先处理请求，未处理或无匹配则 forward 到 MessageHandler。
    """

    def __init__(self, config: GatewayServerConfig, router) -> None:
        super().__init__(config, router)
        self.config = config
        self.bus = router
        self._server = None
        self._running = False
        self._on_message_cb = None
        self._clients: set[Any] = set()
        # V2: key 升级为 (channel_id, session_id, agent_ref_str|None)，
        # 兼容旧 2-tuple key（agent_ref 为 None 时回退）。
        self._request_to_client: dict[tuple, Any] = {}
        self._session_to_client: dict[tuple, Any] = {}
        # ACP 延迟绑定：session 在握手期先挂起，等首个 agent 请求 promote。
        self._pending_session_clients: dict[tuple[str, str], Any] = {}
        self.message_handler_ref = None
        self._acp_bridge = AcpGatewayBridge(
            self._dispatch_on_message,
            bind_session_client=self._bind_acp_session_client,
            channel_id="acp",
            idle_finalize_seconds=lambda: _PROMPT_IDLE_FINALIZE_SECONDS,
        )
        self._install_default_route_hooks()

    @staticmethod
    def _extract_ws_user_id(ws: Any) -> str | None:
        """从 WebSocket 握手 Header 或 URL 查询参数读取 user_id。

        优先从 X-User-Id Header 读取（TUI / 代理注入场景）；
        浏览器 ``new WebSocket()`` 无法设置自定义 Header，回退到 URL query string。
        """
        headers = (
            getattr(getattr(ws, "request", None), "headers", None)
            or getattr(ws, "request_headers", None)
        )
        raw = get_header_value(headers, "X-User-Id")
        if raw is not None:
            text = str(raw).strip()
            if text and _SAFE_USER_ID_RE.match(text):
                return text

        raw_path = (
            getattr(ws, "path", "")
            or getattr(getattr(ws, "request", None), "path", "")
        )
        if raw_path:
            parsed = urlparse(raw_path)
            qs = parse_qs(parsed.query)
            user_ids = qs.get("user_id", [])
            if user_ids:
                candidate = str(user_ids[0]).strip()
                if candidate and _SAFE_USER_ID_RE.match(candidate):
                    return candidate
        return None

    @staticmethod
    def _connection_user_id(ws: Any) -> str | None:
        """返回连接建立时缓存的 user_id（来自握手 Header）。"""
        uid = getattr(ws, "_gateway_user_id", None)
        if uid is None:
            return None
        text = str(uid).strip()
        return text or None

    @staticmethod
    def _connection_agent_type(ws: Any) -> str:
        """返回当前 WS 连接绑定的 agent_type（缺省 jiuwenswarm）。"""
        raw = getattr(ws, "_gateway_agent_type", None)
        text = str(raw or "jiuwenswarm").strip().lower()
        return text or "jiuwenswarm"

    @staticmethod
    def _invoke_local_handler(
        handler: Callable[..., Awaitable[None]],
        ctx: _LocalHandlerContext,
    ) -> Awaitable[None]:
        kwargs: dict[str, Any] = {}
        if "user_id" in inspect.signature(handler).parameters:
            kwargs["user_id"] = ctx.user_id
        return handler(ctx.ws, ctx.req_id, ctx.params, ctx.session_id, **kwargs)

    @staticmethod
    def _client_route_key(
        channel_id: str | None,
        scoped_id: str | None,
        agent_ref: Any = None,
    ) -> tuple | None:
        """构造客户端路由键。

        V2: 可选 agent_ref 作为第三维，支撑同 session 多 agent_ref 共存（场景 2）。
        传入 agent_ref 时返回 (channel_id, scoped_id, agent_ref_str) 三元组；
        不传时返回 (channel_id, scoped_id) 二元组，兼容旧路径。
        """
        channel = str(channel_id or "").strip()
        scope = str(scoped_id or "").strip()
        if not channel or not scope:
            return None
        if agent_ref is not None:
            ar_str = ""
            if hasattr(agent_ref, "mode") and hasattr(agent_ref, "id"):
                ar_str = f"{agent_ref.mode}:{agent_ref.id}"
            elif isinstance(agent_ref, dict):
                ar_str = f"{agent_ref.get('mode', '')}:{agent_ref.get('id', '')}"
            elif isinstance(agent_ref, str) and agent_ref.strip():
                ar_str = agent_ref.strip()
            if ar_str:
                return (channel, scope, ar_str)
        return (channel, scope)

    def _find_channel_clients(self, channel_id: str) -> list[Any]:
        return [
            client_ws for key, client_ws in self._session_to_client.items()
            if isinstance(key, tuple) and key[0] == channel_id and not getattr(client_ws, "closed", False)
        ]

    def _lookup_client(
        self,
        table: dict[tuple, Any],
        channel_id: str | None,
        scope: str | None,
        agent_ref: Any = None,
    ) -> Any | None:
        """按 (channel, scope, agent_ref?) 查找客户端 ws，2↔3 元组双向兜底。

        - 优先精确匹配（agent_ref 非空时 3 元组，否则 2 元组）。
        - 3 元组 MISS → 降级 2 元组（响应侧丢 agent_ref 时仍能命中）。
        - 2 元组 MISS → 升级扫描所有以 (channel, scope) 开头的 3 元组
          （注册侧有 agent_ref、查找侧丢 agent_ref 时仍能命中）。
        - 裸 scope 字符串兜底。
        防御性双向兜底，避免链路某环节丢 agent_ref 导致响应 dropped（设计 §6.3）。
        """
        key = self._client_route_key(channel_id, scope, agent_ref)
        if key is None:
            return None
        ws = table.get(key)
        if ws is not None:
            return ws
        # 3 → 2 降级
        if len(key) >= 3:
            ws = table.get((key[0], key[1]))
            if ws is not None:
                return ws
        # 2 → 3 升级扫描
        if len(key) == 2:
            for k, v in table.items():
                # (channel, scope, agent_ref) 三元组，前两段与 (channel, scope) 匹配
                if isinstance(k, tuple) and len(k) == 3 and k[:2] == key:
                    return v
        # 裸 scope 兜底
        return table.get(scope)

    def is_session_bound_to_client(self, channel_id: str, session_id: str, ws: Any) -> bool:
        session_key = self._client_route_key(channel_id, session_id)
        if session_key is None:
            return False
        return self._session_to_client.get(session_key) is ws

    def get_active_session_ids(
        self,
        channel_id: str,
        exclude_ws: Any = None,
    ) -> set[str]:
        """返回在指定 channel 下、仍处于活跃连接绑定的 session_id 集合。

        用于 session.list 标记 active_in_window，供前端在 /resume 前拦截冲突会话。
        排除 exclude_ws（通常是发起 session.list 请求的连接本身），并跳过已关闭的 ws，
        与实时防线（forward 阶段 SESSION_IN_USE 检查）口径保持一致。
        """
        active: set[str] = set()
        for key, client_ws in self._session_to_client.items():
            if not isinstance(key, tuple) or len(key) < 2:
                continue
            if key[0] != channel_id:
                continue
            if client_ws is exclude_ws:
                continue
            if bool(getattr(client_ws, "closed", False)):
                continue
            session_id = key[1]
            if isinstance(session_id, str) and session_id:
                active.add(session_id)
        return active

    @staticmethod
    def _extract_routing_session_id(msg, *, include_top_level: bool = True) -> str | None:
        """Best-effort session id from message fields for outbound event routing."""
        if include_top_level:
            sid = getattr(msg, "session_id", None)
            if sid is not None and str(sid).strip():
                return str(sid).strip()
        payload = getattr(msg, "payload", None)
        if isinstance(payload, dict):
            sid = payload.get("session_id")
            if sid is not None and str(sid).strip():
                return str(sid).strip()
        params = getattr(msg, "params", None)
        if isinstance(params, dict):
            sid = params.get("session_id")
            if sid is not None and str(sid).strip():
                return str(sid).strip()
        return None

    @staticmethod
    def _ws_is_open(ws: Any) -> bool:
        return ws is not None and not bool(getattr(ws, "closed", False))

    async def _send_frame_to_ws(
        self,
        ws: Any,
        frame: dict[str, Any],
        *,
        channel_id: str | None,
        request_id: str | None,
        session_id: str | None,
    ) -> bool:
        if not self._ws_is_open(ws):
            return False
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
            return True
        except ConnectionClosed:
            logger.info(
                "[GatewayServer] WebSocket closed while sending: channel_id=%s session_id=%s id=%s",
                channel_id,
                session_id,
                request_id,
            )
            return False

    def on_message(self, callback) -> None:
        self._on_message_cb = callback

    async def _dispatch_on_message(self, msg) -> bool:
        if self._on_message_cb is None:
            return False
        result = self._on_message_cb(msg)
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    def _bind_acp_session_client(self, session_id: str, ws: Any, agent_ref: Any = None) -> None:
        session_key = self._client_route_key("acp", session_id, agent_ref)
        if session_key is not None:
            self._session_to_client[session_key] = ws

    async def _bind_route_session_client(
        self,
        route: RouteConfig,
        session_id: str,
        ws: Any,
    ) -> bool:
        session_key = self._client_route_key(route.channel_id, session_id)
        if session_key is None:
            return False
        existing_ws = self._session_to_client.get(session_key)
        if (
            existing_ws is not None
            and existing_ws is not ws
            and not bool(getattr(existing_ws, "closed", False))
        ):
            if self._ws_is_open(ws):
                self._pending_session_clients[session_key] = ws
            return False
        self._session_to_client[session_key] = ws
        if self._pending_session_clients.get(session_key) is ws:
            self._pending_session_clients.pop(session_key, None)
        if existing_ws is ws or route.session_bind_handler is None:
            return True
        try:
            result = route.session_bind_handler(route.channel_id, session_id)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning(
                "GatewayServer session bind handler failed: channel_id=%s session_id=%s",
                route.channel_id,
                session_id,
                exc_info=True,
            )
        return True

    async def _promote_pending_session_client(
        self,
        route: RouteConfig,
        session_key: tuple[str, str],
    ) -> None:
        pending_ws = self._pending_session_clients.pop(session_key, None)
        if not self._ws_is_open(pending_ws):
            return
        existing_ws = self._session_to_client.get(session_key)
        if (
            existing_ws is not None
            and existing_ws is not pending_ws
            and self._ws_is_open(existing_ws)
        ):
            return
        self._session_to_client[session_key] = pending_ws
        if route.session_bind_handler is None:
            return
        try:
            result = route.session_bind_handler(route.channel_id, session_key[1])
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning(
                "GatewayServer pending session bind handler failed: channel_id=%s session_id=%s",
                route.channel_id,
                session_key[1],
                exc_info=True,
            )

    def _get_message_handler(self):
        return self.message_handler_ref

    def _install_default_route_hooks(self) -> None:
        for route in self.config.routes.values():
            if route.channel_id != "acp":
                continue
            if route.inbound_interceptor is not None and route.outbound_interceptor is not None:
                continue
            route.inbound_interceptor = route.inbound_interceptor or self._acp_bridge.inbound_intercept
            route.outbound_interceptor = route.outbound_interceptor or self._acp_bridge.outbound_intercept
            route.cleanup_handler = route.cleanup_handler or self._acp_bridge.cleanup

    def _resolve_route(self, request_path: str) -> tuple[RouteConfig | None, str]:
        """按精确路径匹配路由；支持常见变体（如尾部斜杠）以避免客户端握手失败。"""
        routes = self.config.routes
        p = (request_path or "").strip()
        if not p:
            return None, request_path
        if p in routes:
            return routes[p], p
        if p != "/" and p.endswith("/") and p.rstrip("/") in routes:
            return routes[p.rstrip("/")], p.rstrip("/")
        if not p.endswith("/") and f"{p}/" in routes:
            return routes[f"{p}/"], f"{p}/"
        return None, p

    async def wait_until_closed(self) -> None:
        """阻塞至底层 WebSocket 服务完全关闭（与 :meth:`start` 配对供主循环持有任务）。"""
        if self._server is None:
            return
        await self._server.wait_closed()

    def register_local_handler(self, path: str, method: str, handler: Callable[..., Awaitable[None]]) -> None:
        """为指定路径注册本地方法 handler。"""
        route = self.config.routes.get(path)
        if route is None:
            route = RouteConfig(path=path, channel_id=path.strip("/"))
            self.config.routes[path] = route
        route.local_handlers[method] = handler

    async def send_response(
            self,
            ws: Any,
            req_id: str,
            *,
            ok: bool,
            payload: dict[str, Any] | None = None,
            error: str | None = None,
            code: str | None = None,
    ) -> None:
        """向指定客户端发送 res 帧（供本地 handler 使用）。"""
        frame: dict[str, Any] = {
            "type": "res",
            "id": req_id,
            "ok": ok,
            "payload": payload or {},
        }
        if not ok:
            frame["error"] = error or "request failed"
            if code:
                frame["code"] = code
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            logger.debug("send_response failed (client disconnected?)", exc_info=True)

    async def send_event(
            self,
            ws: Any,
            event: str,
            payload: dict[str, Any],
    ) -> None:
        """向指定客户端发送 event 帧（供本地 handler 使用）。"""
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            logger.debug("send_event failed (client disconnected?)", exc_info=True)

    async def start(self) -> None:
        if self._running or not self.config.enabled:
            return
        try:
            from websockets.legacy.server import serve as ws_serve
        except Exception:  # pragma: no cover
            from websockets import serve as ws_serve

        ws_max_size = 8 * 2**20  # 8 MB — matches AgentServer link

        self._server = await ws_serve(
            self._connection_handler,
            self.config.host,
            self.config.port,
            ping_interval=20,
            ping_timeout=600,
            max_size=ws_max_size,
        )
        self._running = True
        paths = ", ".join(self.config.routes.keys())
        logger.info(
            "[App] Gateway server started: ws://%s:%s [%s]",
            self.config.host,
            self.config.port,
            paths,
        )

    async def stop(self) -> None:
        self._running = False
        close_tasks = [client.close(code=1001, reason="server shutdown") for client in list(self._clients)]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._clients.clear()
        self._request_to_client.clear()
        self._session_to_client.clear()
        self._pending_session_clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.info("[App] Gateway server stopped")

    async def send(self, msg, *, routing_target: "RoutingTarget | None" = None) -> None:
        # V2: 提取 agent_ref，优先 3 元组查找，2↔3 双向兜底（_lookup_client）。
        _agent_ref = getattr(msg, "agent_ref", None) or None
        _send_cid = getattr(msg, "channel_id", None)
        _send_sid = getattr(msg, "session_id", None)
        _send_id = getattr(msg, "id", None)
        ws: Any = None
        # 三级级联 fallback 查找投递目标 ws：任一级命中可用（非 None 且未关闭）ws 即短路，
        # 不再执行后续更低优先级的查找。三级依次为：
        #   1) routing_target（team fan_out 投递，经 dispatch_to_session 调用）
        #   2) request_id 精确回响应（常规 res/event 回到发起请求的 ws）
        #   3) session_id 兜底（同 session 任意活跃 ws，或无 session_id 时广播）

        # ── 第 1 级：fan_out 投递（team 模式经 dispatch_to_session 调用）──
        # GatewayServer 不像 BaseWsChannel 维护 ws_id 物理索引，这里按
        # routing_target.routing_keys 的 session_id 维度查 _session_to_client，
        # 命中任一仍活跃的 ws 即投递。保证 TUI/ACP 误入 team 路径时 GodView/
        # mention fan_out 不会因签名不符而静默丢弃。
        if routing_target is not None:
            _rt_keys = getattr(routing_target, "routing_keys", None) or []
            for _rk in _rt_keys:
                _rk_sid = getattr(_rk, "session_id", None)
                if not _rk_sid:
                    continue
                _cand = self._lookup_client(
                    self._session_to_client,
                    getattr(_rk, "channel_id", None) or _send_cid,
                    _rk_sid,
                    getattr(_rk, "agent_ref", None),
                )
                if _cand is not None and not bool(getattr(_cand, "closed", False)):
                    ws = _cand
                    break
        # ── 第 2 级：request_id 精确回响应（回到发起该请求的 ws）──
        if ws is None:
            ws = self._lookup_client(
                self._request_to_client,
                _send_cid,
                _send_id,
                _agent_ref,
            )
        # ── 第 3 级：session_id 兜底（同 session 任意活跃 ws）──
        # 前级返回 None 或已关闭的 ws 时，仍尝试按 session 路由。
        if ws is None or bool(getattr(ws, "closed", False)):
            ws = self._lookup_client(
                self._session_to_client,
                _send_cid,
                _send_sid,
                _agent_ref,
            )
        if ws is None or bool(getattr(ws, "closed", False)):
            if ws is None:
                channel_id = getattr(msg, "channel_id", None)
                # 多 TUI 窗口：有 session_id 时精确路由；无 session_id 时广播（如 cron 推送到 TUI）。
                if channel_id and channel_id != "acp":
                    if msg.type == "res":
                        payload_data = dict(msg.payload or {}) if isinstance(msg.payload, dict) else {}
                        frame = {"type": "res", "id": msg.id, "ok": bool(msg.ok), "payload": payload_data}
                    else:
                        frame = _build_event_frame(msg)

                    session_id = self._extract_routing_session_id(msg, include_top_level=False)
                    if session_id:
                        session_key = self._client_route_key(channel_id, session_id)
                        if session_key:
                            client = self._session_to_client.get(session_key)
                            if client is not None and not bool(getattr(client, "closed", False)):
                                data = json.dumps(frame, ensure_ascii=False)
                                try:
                                    await client.send(data)
                                except Exception:
                                    logger.debug(
                                        "[GatewayServer] session-routed send failed: session_id=%s",
                                        session_id,
                                        exc_info=True,
                                    )
                                return
                    elif not self._extract_routing_session_id(msg, include_top_level=True):
                        clients = self._find_channel_clients(channel_id)
                        if clients:
                            data = json.dumps(frame, ensure_ascii=False)
                            logger.info(
                                "[GatewayServer] broadcast fallback (no session_id): "
                                "channel_id=%s clients=%d id=%s type=%s",
                                channel_id, len(clients), getattr(msg, "id", None), msg.type,
                            )
                            await asyncio.gather(
                                *[c.send(data) for c in clients],
                                return_exceptions=True,
                            )
                            return
                # delivery target 没注册上：附 sess_table_keys 便于反查为何反查不到 ws
                logger.warning(
                    "[GatewayServer] message dropped: no WebSocket client found for"
                    " channel_id=%s session_id=%s id=%s sess_table_keys=%s",
                    getattr(msg, "channel_id", None),
                    getattr(msg, "session_id", None),
                    getattr(msg, "id", None),
                    [str(k) for k in self._session_to_client.keys()],
                )
            else:
                logger.warning(
                    "[GatewayServer] ws already closed, drop: channel_id=%s id=%s session_id=%s ws=%s",
                    _send_cid, _send_id, _send_sid, hex(id(ws)),
                )
            return

        if getattr(msg, "channel_id", None) == "acp":
            handled = await self._acp_bridge.send_message(msg, ws)
            if handled:
                return

        # 让 route 的 outbound_interceptor 有机会拦截
        for route in self.config.routes.values():
            if route.channel_id == msg.channel_id and route.outbound_interceptor is not None:
                try:
                    handled = route.outbound_interceptor(msg, ws)
                    if asyncio.iscoroutine(handled):
                        handled = await handled
                    if handled:
                        return
                except Exception:
                    logger.warning(
                        "GatewayServer outbound interceptor failed: channel_id=%s",
                        msg.channel_id,
                        exc_info=True,
                    )
                break

        if msg.type == "res":
            payload = dict(msg.payload or {}) if isinstance(msg.payload, dict) else {}
            frame: dict[str, Any] = {
                "type": "res",
                "id": msg.id,
                "ok": bool(msg.ok),
                "payload": payload,
            }
            if not msg.ok:
                frame["error"] = str(payload.get("error") or "request failed")
                # 提升 payload.code 为顶层 code(与本地 handler 的 send_response
                # 错误帧结构一致,设计文档 §1.3 前端按顶层 code 分流)
                code_val = payload.get("code")
                if isinstance(code_val, str) and code_val.strip():
                    frame["code"] = code_val.strip()
            await ws.send(json.dumps(frame, ensure_ascii=False))
            return

        event_name = "chat.final"
        if msg.event_type is not None:
            event_name = msg.event_type.value

        if isinstance(msg.payload, dict):
            payload = {**msg.payload}
            payload.setdefault("session_id", msg.session_id)
        else:
            payload = {"session_id": msg.session_id, "content": str(msg.payload or "")}

        frame = {"type": "event", "event": event_name, "payload": payload}
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _connection_handler(self, ws: Any, path: str | None = None) -> None:
        raw_path = path if path is not None else getattr(ws, "path", "")
        parsed = urlparse(raw_path)
        request_path = parsed.path or raw_path
        remote = getattr(ws, "remote_address", None)

        route, matched_path = self._resolve_route(request_path)
        if route is None:
            await ws.close(code=1008, reason=f"unsupported path: {request_path}")
            return

        self._clients.add(ws)

        ws_user_id = self._extract_ws_user_id(ws)
        setattr(ws, "_gateway_user_id", ws_user_id)
        setattr(ws, "_gateway_agent_type", "jiuwenswarm")
        uid_marker = "" if ws_user_id else " uid_empty=yes"
        logger.info(
            "[Gateway] WS handshake X-User-Id: user_id=%r%s channel=%s path=%s",
            ws_user_id,
            uid_marker,
            route.channel_id,
            matched_path,
        )

        # 触发连接钩子（GatewayServer 自身 + 外部 ws_channel，如 TuiChannel 鉴权）
        for hook in self._connect_hooks:
            try:
                result = hook(ws)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # pragma: no cover
                logger.warning(
                    "WebChannel on_connect hook error: %s",
                    format_ws_diagnostics(
                        {"remote": remote, "path": request_path},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )
        ws_channel = getattr(route, "ws_channel", None)
        if ws_channel is not None:
            for hook in getattr(ws_channel, "_connect_hooks", []):
                try:
                    result = hook(ws)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:
                    logger.warning(
                        "%s on_connect hook error: %s",
                        type(ws_channel).__name__,
                        format_ws_diagnostics(
                            {"remote": remote, "path": request_path},
                            describe_ws_peer(ws),
                            describe_ws_exception(e),
                        ),
                    )

        # 上报连接事件
        if route.ws_channel is not None:
            reporter = getattr(route.ws_channel, "report_connect", None)
            if callable(reporter):
                reporter(ws)

        # connection.ack
        try:
            await ws.send(json.dumps({
                "type": "event",
                "event": "connection.ack",
                "payload": {
                    "protocol_version": "1.0",
                    "transport": route.channel_id,
                },
            }, ensure_ascii=False))
        except Exception:
            self._clients.discard(ws)
            return

        normal_close = False
        try:
            async for raw in ws:
                await self._handle_raw_message(ws, raw, matched_path, route)
            normal_close = True
        except ConnectionClosedError:
            logger.info("[App] WebSocket connection closed: channel=%s", route.channel_id)
        finally:
            if normal_close:
                logger.info(
                    "[App] WebSocket connection closed (normal): channel=%s",
                    route.channel_id,
                )
            self._clients.discard(ws)
            stale_request_keys = [
                key for key, client in self._request_to_client.items() if client is ws
            ]
            for request_key in stale_request_keys:
                self._request_to_client.pop(request_key, None)
            stale_session_keys = [
                key for key, client in self._session_to_client.items() if client is ws
            ]
            for session_key in stale_session_keys:
                self._session_to_client.pop(session_key, None)
            stale_pending_session_keys = [
                key for key, client in self._pending_session_clients.items() if client is ws
            ]
            for session_key in stale_pending_session_keys:
                self._pending_session_clients.pop(session_key, None)
            # V2: 委托 ws_channel（tui 的 TuiChannel）摘除死 ws + 清 per-ws writer。
            # 放在 _session_to_client 清理之后、disconnect_handler 之前；ws_channel
            # 反查不依赖 _session_to_client，顺序无强约束，但先摘可避免后续投递命中死 ws。
            if route.ws_channel is not None:
                try:
                    await route.ws_channel.unregister_ws(ws)
                except Exception:
                    logger.warning(
                        "GatewayServer delegate unregister_ws to ws_channel failed: path=%s",
                        request_path, exc_info=True,
                    )
                # 上报断连事件
                reporter = getattr(route.ws_channel, "report_disconnect", None)
                if callable(reporter):
                    reporter(ws)
            if route.disconnect_handler is not None:
                try:
                    # Pass stale_request_keys so the handler can recover session_ids
                    # via in-flight stream bookkeeping even when _session_to_client
                    # was overwritten by a subsequent reconnect on the same session.
                    result = route.disconnect_handler(
                        ws, stale_session_keys, stale_request_keys,
                    )
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.warning(
                        "GatewayServer disconnect handler failed: path=%s",
                        request_path,
                        exc_info=True,
                    )
            elif route.cleanup_handler is not None:
                try:
                    route.cleanup_handler(ws)
                except Exception:
                    logger.warning(
                        "GatewayServer cleanup handler failed: path=%s",
                        request_path,
                        exc_info=True,
                    )
            for session_key in stale_session_keys:
                await self._promote_pending_session_client(route, session_key)

            for hook in self._disconnect_hooks:
                try:
                    result = hook(ws, None)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:  # pragma: no cover
                    logger.warning(
                        "%s on_disconnect hook error: %s",
                    )

    async def _handle_raw_message(self, ws: Any, raw: str, request_path: str, route: RouteConfig) -> None:

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(
                json.dumps(
                    {"type": "res", "id": "", "ok": False, "error": "invalid json"},
                    ensure_ascii=False,
                )
            )
            return

        if route.channel_id == "acp":
            handled = await self._acp_bridge.handle_jsonrpc_request(ws, data)
            if handled:
                return

        if route.channel_id == "acp" and self._acp_bridge.is_jsonrpc_request(data):
            return

        # route 级别的原始帧拦截（如 ACP JSON-RPC response）
        if route.inbound_interceptor is not None:
            try:
                handled = route.inbound_interceptor(ws, data)
                if asyncio.iscoroutine(handled):
                    handled = await handled
                if handled:
                    return
            except Exception:
                logger.warning(
                    "GatewayServer inbound interceptor failed: path=%s",
                    request_path,
                    exc_info=True,
                )

        if not isinstance(data, dict) or data.get("type") != "req":
            await ws.send(
                json.dumps(
                    {"type": "res", "id": "", "ok": False, "error": "invalid request"},
                    ensure_ascii=False,
                )
            )
            return

        req_id = str(data.get("id") or "").strip()
        method = str(data.get("method") or "").strip()
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        if not req_id or not method:
            await ws.send(
                json.dumps(
                    {"type": "res", "id": req_id, "ok": False, "error": "invalid request"},
                    ensure_ascii=False,
                )
            )
            return

        explicit_session_id = bool(str(params.get("session_id") or "").strip())
        session_id = (str(params.get("session_id") or "").strip()) or req_id
        session_key = self._client_route_key(route.channel_id, session_id) if explicit_session_id else None

        req_user_id = self._connection_user_id(ws)

        # 1. forward 优先：方法在 forward_methods 中则转发到 MessageHandler
        if method in route.forward_methods:
            req_method = None
            for item in ReqMethod:
                if item.value == method:
                    req_method = item
                    break

            if req_method is None:
                await ws.send(
                    json.dumps(
                        {"type": "res", "id": req_id, "ok": False, "error": f"unknown method: {method}"},
                        ensure_ascii=False,
                    )
                )
                return

            # V2: 提取 agent_ref 用于路由键（支撑场景 2 同 session 切 mode 不串窗）。
            # 客户端未发送 agent_ref 时不合成——保持 2 元组注册，与响应查找侧
            # （AgentServer 普通模式不回带 agent_ref → msg.agent_ref=None → 2 元组）
            # 对称，避免 3/2 元组不匹配导致响应消息被丢弃（设计 §1.1 非 team 零改动）。
            _agent_ref = params.get("agent_ref")

            request_key = self._client_route_key(route.channel_id, req_id, _agent_ref)
            # 升级 session_key 为 3 元组（含 agent_ref），与 _session_to_client 键一致。
            session_key = (
                self._client_route_key(route.channel_id, session_id, _agent_ref)
                if explicit_session_id else None
            )
            # 检测 session 是否已在其他窗口打开：若已绑定到另一个仍活跃的连接，
            # 拒绝当前请求，避免多窗口 session 冲突（事件路由串台、binding 被覆盖）。
            # 前端 session.list 的 active_in_window 标记是"事后快照"，此处是实时防线。
            if session_key is not None:
                existing_ws = self._session_to_client.get(session_key)
                if (
                    existing_ws is not None
                    and existing_ws is not ws
                    and not bool(getattr(existing_ws, "closed", False))
                ):
                    logger.warning(
                        "GatewayServer reject %s: session %s already active in another %s connection",
                        method, session_id, route.channel_id,
                    )
                    await self.send_response(
                        ws, req_id, ok=False,
                        error=f"Session {session_id} is already active in another window. Close that window first.",
                        code="SESSION_IN_USE",
                    )
                    return
            if request_key is not None:
                self._request_to_client[request_key] = ws
            if session_key is not None:
                await self._bind_route_session_client(route, session_id, ws)

            default_mode = Mode.CODE_NORMAL if route.channel_id == "tui" else Mode.AGENT
            mode = Mode.from_raw(params.get("mode"), default=default_mode)

            # 确保 mode 被设置到 params 中，以便后续转发到 AgentServer
            params = dict(params)
            params.setdefault("mode", mode.value)

            # 连接级 agent_type 为 SSOT，注入后续转发请求
            # （3rdagent.switch 由 TUI local_handler 写回 ws._gateway_agent_type）
            params["agent_type"] = self._connection_agent_type(ws)

            # V2: agent_ref 全链路透传（阶段2）。
            # tui 客户端从不发 agent_ref（_agent_ref 恒 None）→ 按 mode/agent_id 合成 AgentRef，
            # 用于：(1) 委托 _register 的 RoutingKey.agent_ref；(2) msg.agent_ref 经 E2A 透传到
            # AgentServer，chunk 回带同 agent_ref；(3) _maybe_register_godview 用 msg.agent_ref 构造
            # GodView RoutingKey.agent_ref（与入站注册同源，routing_keys 兜底能命中）。
            # acp 等非 ws_channel route 不合成（保留原 _agent_ref，走各自路径）。
            from jiuwenswarm.gateway.routing.keys import AgentRef as _AgentRef

            _resolved_agent_ref = _agent_ref
            if route.ws_channel is not None and not isinstance(_resolved_agent_ref, _AgentRef):
                _agent_id_raw = str(params.get("agent_id") or "default").strip() or "default"
                _resolved_agent_ref = _AgentRef(mode=mode.value, id=_agent_id_raw)

            # V2: 委托 ws 注册进 route.ws_channel（tui 的 TuiChannel）的五维索引。
            # GatewayServer 仍保留 _session_to_client/_request_to_client 用于入站响应反查
            # （send_response / local handler 回包），但出站 chunk 的精确路由改由
            # TuiChannel 的 _ws_by_id（物理寻址）/ _clients_by_key（五维逻辑）承担。
            # 同步把 ws_id 注入 metadata，供 _maybe_register_godview 构造带真 ws_id 的
            # TuiDeliveryTarget（修掉原 tui _kind="group" + ws_id 恒空导致投递不到）。
            _ws_id_for_metadata = ""
            if route.ws_channel is not None:
                from jiuwenswarm.gateway.routing.keys import RoutingKey

                # user_id 取 "tui"：与 _maybe_register_godview 的 _user 兜底（... or _ch）
                # 同源，保证 GodView 订阅的 RoutingKey.user_id 与此处入站注册一致。
                _tui_rk = RoutingKey(
                    user_id="tui",
                    channel_id=route.channel_id,
                    app_id="default",
                    agent_ref=_resolved_agent_ref,
                    session_id=session_id,
                )
                try:
                    await route.ws_channel.register_ws(ws, _tui_rk)
                    _ws_id_for_metadata = getattr(ws, "_jiuwen_ws_id", "") or ""
                except Exception:
                    logger.warning(
                        "GatewayServer delegate _register to ws_channel failed: channel_id=%s session_id=%s",
                        route.channel_id, session_id, exc_info=True,
                    )

            # 从 params 中提取 cwd/project_dir，注入到 metadata 中
            # cwd 供 message_handler 解析 @file 引用；project_dir 供 session.list 按项目过滤
            metadata = {"method": method}
            if _ws_id_for_metadata:
                metadata["ws_id"] = _ws_id_for_metadata
            cwd = params.get("cwd")
            if cwd and isinstance(cwd, str) and cwd.strip():
                metadata["cwd"] = cwd.strip()
            project_dir = params.get("project_dir")
            if project_dir and isinstance(project_dir, str) and project_dir.strip():
                metadata["project_dir"] = project_dir.strip()
                # 记录会话首条消息时所在的 git 分支，供 /resume 按分支过滤（Ctrl+B）。
                # 非 git/detached/失败时为哨兵 "HEAD"。
                from jiuwenswarm.common.utils import resolve_git_branch

                metadata["git_branch"] = resolve_git_branch(project_dir.strip())
            client_timeout_ms = coerce_client_timeout_ms(data.get("timeout_ms"))
            if route.channel_id == "tui" and client_timeout_ms is not None:
                metadata["client_timeout_ms"] = client_timeout_ms

            is_stream = bool(data.get("is_stream", False))

            msg = Message(
                id=req_id,
                type="req",
                channel_id=route.channel_id,
                session_id=session_id,
                params=params,
                timestamp=time.time(),
                ok=True,
                req_method=req_method,
                mode=mode,
                metadata=metadata,
                is_stream=is_stream,
                # V2: 透传 agent_ref 到请求 Message，经 E2A 线协议传到 AgentServer，
                # 响应侧 chunk/response 回带，gateway 查找侧 3 元组匹配（设计 §6.3）。
                # 阶段2：tui ws_channel route 用合成的 _resolved_agent_ref（与 _register /
                # GodView 同源）；非 ws_channel route 保留原 _agent_ref。
                agent_ref=_resolved_agent_ref,
                user_id=req_user_id,
            )

            if self._on_message_cb is not None:
                result = self._on_message_cb(msg)
                if asyncio.iscoroutine(result):
                    await result

            # ACP route may receive legacy ``type=req`` frames from some clients.
            # They should be forwarded upstream without falling through to the
            # generic "unknown method" error path.
            if route.channel_id == "acp":
                return

            # 如果在 forward_no_local_handler_methods 中，不需要本地 ack
            if method in route.forward_no_local_handler_methods:
                return

        # 2. 本地 handler：发 ack 或纯本地处理
        local_handler = route.local_handlers.get(method)
        if local_handler is not None:
            if session_key is not None:
                await self._bind_route_session_client(route, session_id, ws)
            try:
                await self._invoke_local_handler(
                    local_handler,
                    _LocalHandlerContext(
                        ws=ws,
                        req_id=req_id,
                        params=params,
                        session_id=session_id,
                        user_id=req_user_id,
                    ),
                )
            except Exception as e:
                ws_closed = bool(getattr(ws, "closed", False))
                if ws_closed:
                    logger.warning("GatewayServer local handler aborted on closed ws (%s): %s", method, e)
                    return
                logger.error("GatewayServer local handler error (%s): %s", method, e)
                try:
                    await self.send_response(
                        ws, req_id, ok=False,
                        error=f"handler error: {e}", code="INTERNAL_ERROR",
                    )
                except Exception:
                    logger.debug(
                        "GatewayServer failed to send handler error response: method=%s id=%s",
                        method,
                        req_id,
                        exc_info=True,
                    )
            return

        # 3. 无 forward 也无本地 handler
        await ws.send(
            json.dumps(
                {"type": "res", "id": req_id, "ok": False, "error": f"unknown method: {method}"},
                ensure_ascii=False,
            )
        )


def _build_acp_route_binding(
        *,
        path: str,
        channel_id: str,
        forward_methods: frozenset[str],
        forward_no_local_handler_methods: frozenset[str],
        on_message_cb,
) -> GatewayRouteBinding:
    return GatewayRouteBinding(
        path=path,
        channel_id=channel_id,
        forward_methods=forward_methods,
        forward_no_local_handler_methods=forward_no_local_handler_methods,
    )


def _build_route_config_map(bindings: list[GatewayRouteBinding]) -> dict[str, RouteConfig]:
    return {
        binding.path: RouteConfig(
            path=binding.path,
            channel_id=binding.channel_id,
            forward_methods=binding.forward_methods,
            forward_no_local_handler_methods=binding.forward_no_local_handler_methods,
            inbound_interceptor=binding.inbound_interceptor,
            outbound_interceptor=binding.outbound_interceptor,
            cleanup_handler=binding.cleanup_handler,
            disconnect_handler=binding.disconnect_handler,
            session_bind_handler=binding.session_bind_handler,
            ws_channel=binding.ws_channel,
        )
        for binding in bindings
    }


async def _run(
        agent_server_url: str,
        web_host: str,
        web_port: int,
        web_path: str,
        web_dual_protocol: bool = True,
) -> None:
    from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import A2AChannel, A2AChannelConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.dingtalk_connect import DingTalkChannel, \
        DingTalkConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import FeishuChannel, FeishuConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.whatsapp.whatsapp_connect import WhatsAppChannel, \
        WhatsAppChannelConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.wechat.wechat_connect import WechatChannel, WechatConfig
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
        XiaoyiChannel, XiaoyiChannelConfig,
    )
    from jiuwenswarm.gateway.channel_manager.im_platforms.telegram.telegram_connect import TelegramChannel, \
        TelegramChannelConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.discord.discord_connect import DiscordChannel, \
        DiscordChannelConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import SlackChannel, \
        SlackChannelConfig
    from jiuwenswarm.gateway.channel_manager.im_platforms.wecom.wecom_connect import WecomChannel, WecomConfig
    from jiuwenswarm.gateway.channel_manager.protocol.ssh.ssh_connect import SshChannel, SshChannelConfig
    from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import KeyRegistry
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.cleanup import start_background_cleanup
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    from jiuwenswarm.gateway.cron import CronController, CronJobStore, CronSchedulerService
    from jiuwenswarm.gateway.heartbeat.heartbeat import GatewayHeartbeatService, HeartbeatConfig
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        WebHandlersBindParams,
        _DummyBus,
        _CONFIG_SET_ENV_MAP,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        _FORWARD_REQ_METHODS,
        _normalize_feishu_conf,
        _normalize_xiaoyi_conf,
        _register_web_handlers,
    )
    from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
        CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
        CLI_FORWARD_REQ_METHODS,
        CliRouteBindParams,
        build_cli_route_binding,
    )
    from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel, TuiChannelConfig
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.common.updater import UpdaterService
    from openjiuwen.core.runner import Runner

    logger.info("[App] Gateway starting, connecting AgentServer: %s", agent_server_url)
    restart_request = GatewayRestartRequest()

    callback_framework = Runner.callback_framework
    extension_registry = ExtensionRegistry.create_instance(
        callback_framework=callback_framework,
        config={},
        logger=logger,
    )
    extension_manager = ExtensionManager(registry=extension_registry)
    await extension_manager.load_all_extensions()
    logger.info("[App] extensions loaded: %d", len(extension_manager.list_extensions()))

    max_retries = int(os.getenv("AGENT_CONNECT_RETRY", "20"))
    retry_interval = float(os.getenv("AGENT_CONNECT_RETRY_INTERVAL", "3"))

    # 从扩展注册表获取 AgentServerClient（WebSocket 或 Yuanrong 等扩展实现）
    agent_server_ext = extension_registry.get_agent_server_client_extension()
    if agent_server_ext is not None:
        logger.info("[App] using extension AgentServerClient: %s", agent_server_ext.metadata.name)
        client = agent_server_ext.get_client()
    else:
        client = WebSocketAgentServerClient(ping_interval=20.0, ping_timeout=600.0)

    from jiuwenswarm.gateway.routing.third_agent import get_unsupported_third_agent

    third_agent_ext = extension_registry.get_third_agent_extension()
    if third_agent_ext is not None:
        logger.info("[App] using extension ThirdAgent: %s", third_agent_ext.metadata.name)
        third_agent = third_agent_ext.get_third_agent()
    else:
        third_agent = get_unsupported_third_agent()

    # 如果是 WebSocket 客户端，需要连接；如果是 YuanrongFrontendAgentClient，无需连接
    if isinstance(client, WebSocketAgentServerClient):
        await _connect_with_retry(
            client,
            agent_server_url,
            max_retries=max_retries,
            interval=retry_interval,
        )
    else:
        # YuanrongFrontendAgentClient 是 HTTP 客户端，无需连接
        await client.connect("")

    message_handler = MessageHandler(client)
    await message_handler.start_forwarding()

    # IM Pipeline 初始化（数字分身）
    from jiuwenswarm.gateway.im_pipeline.im_inbound import IMInboundPipeline
    from jiuwenswarm.gateway.im_pipeline.im_outbound import IMOutboundPipeline
    im_inbound = IMInboundPipeline()
    im_outbound = IMOutboundPipeline()
    message_handler.set_inbound_pipeline(im_inbound)
    message_handler.set_outbound_pipeline(im_outbound)

    cron_store = CronJobStore(path=get_cron_jobs_path())
    cron_scheduler = CronSchedulerService(
        store=cron_store,
        agent_client=client,
        message_handler=message_handler,
    )
    cron_controller = CronController.get_instance(store=cron_store, scheduler=cron_scheduler)
    message_handler.set_cron_controller(cron_controller)

    full_cfg: dict[str, Any] = {}
    heartbeat_cfg: dict | None = None
    channels_cfg: dict | None = None
    try:
        full_cfg = get_config()
        message_handler.update_evolution_auto_save(full_cfg)
        heartbeat_cfg = full_cfg.get("heartbeat") if isinstance(full_cfg, dict) else None
        channels_cfg = full_cfg.get("channels") if isinstance(full_cfg, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[App] failed to read heartbeat config from config.yaml, using defaults: %s", e)
        heartbeat_cfg = None
        channels_cfg = None

    client.set_or_update_server_config(
        config=dict(full_cfg or {}),
        env={env_key: (os.getenv(env_key) or "") for env_key in _CONFIG_SET_ENV_MAP.values()},
    )

    if isinstance(heartbeat_cfg, dict):
        cfg_every = heartbeat_cfg.get("every")
        cfg_target = heartbeat_cfg.get("target")
        cfg_active_hours = heartbeat_cfg.get("active_hours")
    else:
        cfg_every = None
        cfg_target = None
        cfg_active_hours = None

    heartbeat_interval = float(
        os.getenv("HEARTBEAT_INTERVAL")
        or (str(cfg_every) if cfg_every is not None else "60")
    )
    heartbeat_timeout = float(os.getenv("HEARTBEAT_TIMEOUT", "30")) if os.getenv("HEARTBEAT_TIMEOUT") else None
    heartbeat_relay_channel = os.getenv("HEARTBEAT_RELAY_CHANNEL_ID") or (
        str(cfg_target) if cfg_target is not None else "web"
    )

    heartbeat_config = HeartbeatConfig(
        interval_seconds=heartbeat_interval,
        timeout_seconds=heartbeat_timeout,
        relay_channel_id=heartbeat_relay_channel,
        active_hours=cfg_active_hours if isinstance(cfg_active_hours, dict) else None,
    )
    heartbeat_service = GatewayHeartbeatService(
        client,
        heartbeat_config,
        message_handler=message_handler,
    )
    await heartbeat_service.start()

    _cleanup_task = start_background_cleanup()

    initial_channels_conf: dict = channels_cfg if isinstance(channels_cfg, dict) else {}
    channel_manager = ChannelManager(message_handler, config=initial_channels_conf)
    # 回填引用：MessageHandler 实例化早于 ChannelManager，广播全局事件时需经它取 web channel。
    message_handler.set_channel_manager(channel_manager)

    updater_service = UpdaterService()
    prewarm_sync_debounce_task: asyncio.Task[None] | None = None

    async def _sync_agent_prewarm_channels() -> None:
        try:
            prewarm_channels = {
                channel
                for channel in channel_manager.enabled_channels
                if channel.lower() not in _AGENT_PREWARM_EXCLUDED_CHANNELS
            }
            env = e2a_from_agent_fields(
                request_id=f"agent-prewarm-sync-{uuid_module.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_PREWARM_SYNC,
                params={
                    "enabled_channels": sorted(prewarm_channels),
                },
            )
            resp = await client.send_request(env)
            if not getattr(resp, "ok", False):
                raise RuntimeError(
                    f"agent.prewarm.sync rejected: {getattr(resp, 'payload', None)}"
                )
        except Exception as exc:  # noqa: BLE001 - prewarm never blocks Gateway config
            logger.warning("[App] agent.prewarm.sync failed (non-fatal): %s", exc)

    async def _periodic_agent_prewarm_sync() -> None:
        while True:
            await asyncio.sleep(60)
            await _sync_agent_prewarm_channels()

    def _schedule_agent_prewarm_sync(
        name: str, *, delay_seconds: float = 1.0
    ) -> None:
        """Coalesce startup/config/channel churn into one settled sync."""
        nonlocal prewarm_sync_debounce_task
        previous = prewarm_sync_debounce_task
        if previous is not None and not previous.done():
            previous.cancel()

        async def _delayed_sync() -> None:
            try:
                await asyncio.sleep(max(0.0, delay_seconds))
                await _sync_agent_prewarm_channels()
            except asyncio.CancelledError:
                return

        prewarm_sync_debounce_task = asyncio.create_task(
            _delayed_sync(), name=name
        )

    async def _on_config_saved(
            updated_env_keys: set[str] | None = None,
            *,
            env_updates: dict[str, str] | None = None,
            config_payload: dict[str, Any] | None = None,
            reload_options: dict[str, Any] | None = None,
    ) -> bool:
        browser_runtime_keys = {
            "MODEL_PROVIDER",
            "MODEL_NAME",
            "API_BASE",
            "API_KEY",
            "VIDEO_PROVIDER",
            "VIDEO_MODEL_NAME",
            "VIDEO_API_BASE",
            "VIDEO_API_KEY",
            "AUDIO_PROVIDER",
            "AUDIO_MODEL_NAME",
            "AUDIO_API_BASE",
            "AUDIO_API_KEY",
            "VISION_PROVIDER",
            "VISION_MODEL_NAME",
            "VISION_API_BASE",
            "VISION_API_KEY",
        }
        _reload_max_retries = 3
        _reload_retry_backoff_base = 2.0  # seconds: 2, 4, 8

        client.set_or_update_server_config(
            config=dict(config_payload or {}),
            env=dict(env_updates or {}),
        )

        # reload 是全局配置热重载，不绑定特定 user。但 faas 沙箱 acquire instance
        # 需要 X-Session-Context（由 envelope.user_id 透传成 sessionCtxID）；user_id 为空
        # 时 faas 拿不到 sessionCtxID，acquire lease 走 init 路径失败
        # （"connect runtime failed"），invocation 60s 超时，首登 config.set 后前端卡死+1001。
        # user_id 还必须是 faas 沙箱配置了可工作区的真实用户：faas 沙箱按 user_id
        # 映射 /home/<user>/.jiuwenswarm 工作区，非配置用户（system/root）建沙箱失败
        # （code 80004 "Failed to create sandbox"）。agentos_test 是 agentos 部署的默认
        # 用户（gateway-config.yaml ssh username），有完整工作区，复用其 warm instance。
        reload_env = e2a_from_agent_fields(
            request_id=f"agent-reload-{uuid_module.uuid4().hex[:8]}",
            channel_id="",
            session_id="sess_reload",
            user_id="agentos_test",
            req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            params={
                # config: full config snapshot after save; Agent should prefer this over local yaml.
                "config": dict(config_payload or {}),
                # env: incremental environment updates; missing keys mean unchanged.
                "env": dict(env_updates or {}),
                **dict(reload_options or {}),
            },
        )

        reload_ok = False
        last_error: Exception | None = None
        for attempt in range(_reload_max_retries + 1):
            try:
                reload_resp = await client.send_request(reload_env)
                if getattr(reload_resp, "ok", False):
                    reload_ok = True
                    break
                err_payload = getattr(reload_resp, "payload", None) or {}
                err_msg = (
                    err_payload.get("error")
                    if isinstance(err_payload, dict)
                    else err_payload
                )
                err_str = str(err_msg or "")
                # ValidationError 是配置格式问题，不需要重试也不需要重启 gateway
                if any(kw in err_str for kw in ("ValidationError", "validation error", "Field required")):
                    logger.warning("[App] agent.reload_config validation error (non-fatal): %s", err_str)
                    return False
                last_error = RuntimeError(f"agent.reload_config rejected: {err_msg}")
            except Exception as e:  # noqa: BLE001
                last_error = e

            if attempt < _reload_max_retries:
                delay = _reload_retry_backoff_base ** (attempt + 1)
                logger.warning(
                    "[App] agent.reload_config attempt %d/%d failed, retrying in %.1fs: %s",
                    attempt + 1, _reload_max_retries + 1, delay, last_error,
                )
                await asyncio.sleep(delay)

        if not reload_ok:
            logger.critical(
                "[App] agent.reload_config failed after %d attempts, scheduling restart: %s",
                _reload_max_retries + 1, last_error,
            )
            _schedule_gateway_restart(restart_request)
            return False

        # AgentServer 已确认热重载成功后，再刷新 Gateway 内存中的值。
        # MessageHandler 的流式 chunk 热路径只读取该值，不重新读盘。
        message_handler.update_evolution_auto_save(config_payload)

        # reload 成功：config 变更后让 agent 侧 prewarm channels 落到新配置。
        _schedule_agent_prewarm_sync(
            "agent-prewarm-sync-after-config",
            delay_seconds=3.0,
        )

        if updated_env_keys and (browser_runtime_keys & set(updated_env_keys)):
            restart_env = e2a_from_agent_fields(
                request_id=f"browser-restart-{uuid_module.uuid4().hex[:8]}",
                channel_id="",
                session_id="sess_reload",
                user_id="agentos_test",
                req_method=ReqMethod.BROWSER_RUNTIME_RESTART,
            )
            try:
                await client.send_request(restart_env)
            except Exception as e:  # noqa: BLE001
                logger.warning("[App] browser runtime restart failed (non-fatal): %s", e)

        # 主动推荐：enabled 变更时同步 proactive.tick job（创建/删除）
        proactive_keys = {
            "proactive_recommendation_enabled",
        }
        if updated_env_keys and (proactive_keys & set(updated_env_keys)):
            try:
                from jiuwenswarm.gateway.cron.proactive_cron_sync import sync_proactive_tick_job
                await sync_proactive_tick_job(cron_controller, config_payload)
            except Exception as e:  # noqa: BLE001  # 兜底：proactive 同步失败不阻断配置保存
                logger.warning("[App] proactive.tick sync on config save failed: %s", e)
        return True

    web_channel = None
    tui_channel = None
    web_config = WebChannelConfig(
        enabled=True,
        host=web_host,
        port=web_port,
        path=web_path,
        dual_protocol=web_dual_protocol,
    )
    web_channel = WebChannel(web_config, _DummyBus())

    # 注入 Git diff 监控注册表(设计文档阶段10):
    # 1. 让 ``_mark_git_watcher_dirty`` 能通过 ``channel.git_watcher_registry`` 唤醒轮询
    # 2. 通过 ``set_channel`` 让 registry 拿到 send_event 的发送句柄
    from jiuwenswarm.server.runtime.session.git_diff_watcher import (
        get_git_diff_watcher_registry,
    )
    _git_watcher_registry = get_git_diff_watcher_registry()
    web_channel.git_watcher_registry = _git_watcher_registry
    _git_watcher_registry.set_channel(web_channel)

    _register_web_handlers(
        WebHandlersBindParams(
            channel=web_channel,
            agent_client=client,
            message_handler=message_handler,
            channel_manager=channel_manager,
            on_config_saved=_on_config_saved,
            heartbeat_service=heartbeat_service,
            cron_controller=cron_controller,
            updater_service=updater_service,
        )
    )

    def _make_norm_and_forward(
            forward_methods: set[str] | frozenset[str],
            no_local_methods: set[str] | frozenset[str],
            source_label: str,
    ):
        async def _norm_and_forward(msg: Message) -> bool:
            method_val = getattr(getattr(msg, "req_method", None), "value", None) or ""
            if method_val not in forward_methods:
                return False
            normalized = _normalize_gateway_message(msg)
            # session.create 主路径注入 work_mode 归一化(与 fallback _session_create
            # 共用同一 helper resolve_session_work_mode_params,保持主路径/fallback 一致):
            # 成功时写回归一化后的 project_id/project_dir/work_mode 到 params,
            # 转发到 AgentServer 后由其 session.create 处理逻辑使用;
            # 失败时(非法 work_mode)不写回,保留原始 params 由 AgentServer 或
            # fallback _session_create 返回 BAD_REQUEST。
            if method_val == "session.create":
                _inject_session_work_mode(normalized)
            await channel_manager.deliver_to_message_handler(normalized)
            logger.info("[App] %s 入站 -> MessageHandler: id=%s channel_id=%s", source_label, msg.id, msg.channel_id)
            if method_val in no_local_methods:
                return True
            return False

        return _norm_and_forward

    web_norm_and_forward = _make_norm_and_forward(
        _FORWARD_REQ_METHODS,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        "Web",
    )
    channel_manager.register_channel_with_inbound(web_channel, web_norm_and_forward)

    # ── V2: TUI 独立 Channel（出站契约 + 五维索引）──
    # GatewayServer 仍是 /tui ws 宿主 + 入站帧解析 + local handler 派发（install 仍挂
    # gateway_server）；TuiChannel 只接管出站 send 与 ws 五维索引，被 GatewayServer
    # 在 forward 分支委托 register_ws/unregister_ws。移除原 register_external_channel("tui",
    # gateway_server)，tui 出站不再走 GatewayServer.send（其 routing_target 反查缺五维索引）。
    tui_channel = TuiChannel(TuiChannelConfig(enabled=True), _DummyBus())
    tui_norm_and_forward = _make_norm_and_forward(
        CLI_FORWARD_REQ_METHODS,
        CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
        "TUI",
    )
    channel_manager.register_channel_with_inbound(tui_channel, tui_norm_and_forward)

    # Web/TUI 注册后再订阅连接钩子，否则 get_channel 拿不到 channel。
    subscribe_fn = getattr(client, "set_channel_manager", None)
    if callable(subscribe_fn):
        subscribe_fn(channel_manager)

    acp_inbound_server = _InboundGatewayServer(
        lambda msg: _normalize_and_forward_message(msg, channel_manager)
    )
    await acp_inbound_server.start()

    route_bindings = [
        _build_acp_route_binding(
            path="/acp",
            channel_id="acp",
            forward_methods=_FORWARD_REQ_METHODS,
            forward_no_local_handler_methods=_FORWARD_NO_LOCAL_HANDLER_METHODS,
            on_message_cb=acp_inbound_server.handle_message,
        ),
        build_cli_route_binding(
            CliRouteBindParams(
                agent_client=client,
                message_handler=message_handler,
                third_agent=third_agent,
                on_config_saved=_on_config_saved,
                path="/tui",
                channel_id="tui",
                cron_controller=cron_controller,
                ws_channel=tui_channel,
            )
        ),
    ]

    gateway_server_config = GatewayServerConfig(
        enabled=True,
        host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.getenv("GATEWAY_PORT", "19001")),
        routes=_build_route_config_map(route_bindings),
    )
    gateway_server = GatewayServer(gateway_server_config, _DummyBus())
    gateway_server.message_handler_ref = message_handler
    for binding in route_bindings:
        route_config = gateway_server_config.routes[binding.path]
        # tui 出站已由 TuiChannel 接管（register_channel_with_inbound），
        # 不再把 gateway_server 注册为 tui channel；acp 等仍走 gateway_server。
        # 但 install（register_cli_handlers 注册 local handler 如 _chat_send）
        # 仍须执行——local handler 依赖 gateway_server 上的 session/request 索引。
        if route_config.channel_id != "tui":
            channel_manager.register_external_channel(route_config.channel_id, gateway_server)
        if binding.install is not None:
            binding.install(gateway_server)
    gateway_server.on_message(acp_inbound_server.handle_message)

    a2a_server_enabled = str(os.getenv("A2A_SERVER_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    a2a_channel = A2AChannel(
        A2AChannelConfig(
            enabled=a2a_server_enabled,
            host=str(os.getenv("A2A_SERVER_HOST", "127.0.0.1")).strip() or "127.0.0.1",
            port=int(os.getenv("A2A_SERVER_PORT", "19100")),
            rpc_path=str(os.getenv("A2A_SERVER_PATH", "/a2a")).strip() or "/a2a",
            protocol_version=str(os.getenv("A2A_SERVER_PROTOCOL_VERSION", "1.0.0")).strip() or "1.0.0",
            card_path=str(
                os.getenv("A2A_SERVER_CARD_PATH", "/.well-known/agent-card.json")
            ).strip()
                      or "/.well-known/agent-card.json",
            extended_card_path=str(
                os.getenv("A2A_SERVER_EXTENDED_CARD_PATH", "/agent/authenticatedExtendedCard")
            ).strip()
                               or "/agent/authenticatedExtendedCard",
            app_name=str(
                os.getenv("A2A_SERVER_APP_NAME", "JiuwenSwarm Gateway A2A Server")
            ).strip()
                     or "JiuwenSwarm Gateway A2A Server",
            app_description=str(
                os.getenv("A2A_SERVER_APP_DESCRIPTION", "A2A ingress for JiuwenSwarm Gateway")
            ).strip()
                            or "A2A ingress for JiuwenSwarm Gateway",
            app_version=str(
                os.getenv("A2A_SERVER_APP_VERSION", "0.1.0")
            ).strip()
                        or "0.1.0",
            expose_reasoning=str(os.getenv("A2A_SERVER_EXPOSE_REASONING", "true")).strip().lower()
                             not in {"0", "false", "no", "off"},
        ),
        _DummyBus(),
    )
    channel_manager.register_channel(a2a_channel)
    a2a_task = asyncio.create_task(a2a_channel.start(), name="a2a-channel")
    if a2a_server_enabled:
        # Keep gateway startup non-blocking; surface background A2A boot failures with actionable logs.
        def _on_a2a_task_done(task: asyncio.Task) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[App] A2A server failed to start: %s. "
                    "If A2A is enabled, install optional dependency with "
                    "`uv sync --extra a2a` or `pip install \"jiuwenswarm[a2a]\"`.",
                    exc,
                )

        a2a_task.add_done_callback(_on_a2a_task_done)

    feishu_channel = None
    feishu_task = None
    feishu_enterprise_channels: dict[str, FeishuChannel] = {}
    feishu_enterprise_tasks: dict[str, asyncio.Task] = {}
    xiaoyi_channel = None
    xiaoyi_task = None
    dingtalk_channel = None
    dingtalk_task = None
    telegram_channel = None
    telegram_task = None
    discord_channel = None
    discord_task = None
    slack_channel = None
    slack_task = None
    whatsapp_channel = None
    whatsapp_task = None
    wecom_channel = None
    wecom_task = None
    wechat_channel = None
    wechat_task = None
    ssh_channel = None
    ssh_task = None

    def _set_agentos_ssh_key_issuer(
        issuer,
        *,
        ephemeral_key_ttl_sec: float = 300.0,
    ) -> None:
        """Inject/clear SshChannel as ephemeral key issuer on AgentOSRouter extension."""
        setter = getattr(agent_server_ext, "set_key_issuer", None)
        if not callable(setter):
            return
        try:
            setter(issuer, ephemeral_key_ttl_sec=ephemeral_key_ttl_sec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[App] failed to set AgentOS SSH key issuer: %s", exc)

    _last_channels_conf: dict = {}

    def _should_restart_channel(channel_name: str, old_conf: dict, new_conf: dict) -> bool:
        old_channel_conf = old_conf.get(channel_name) if isinstance(old_conf, dict) else None
        new_channel_conf = new_conf.get(channel_name) if isinstance(new_conf, dict) else None
        if (old_channel_conf is None) != (new_channel_conf is None):
            return True
        if old_channel_conf is None:
            return False
        return old_channel_conf != new_channel_conf

    async def _stop_channel(channel, task, channel_name: str, background_wait: bool = False) -> None:
        if task is not None:
            task.cancel()
            if background_wait:

                async def wait_cancel():
                    try:
                        await task
                    except (TypeError, asyncio.CancelledError):
                        logger.info("[App] cancelled previous %sChannel task", channel_name.capitalize())
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "[App] ignored exception while waiting for previous %sChannel task: %s",
                            channel_name.capitalize(),
                            e,
                        )

                asyncio.create_task(wait_cancel(), name=f"wait_{channel_name}_cancel")
            else:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[App] timeout while waiting for %sChannel task cancellation",
                        channel_name.capitalize(),
                    )
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[App] ignored exception while waiting for previous %sChannel task: %s",
                        channel_name.capitalize(),
                        e,
                    )

        if channel is not None:
            try:
                await asyncio.wait_for(channel.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[App] timeout while stopping %sChannel", channel_name.capitalize())
            except Exception as e:  # noqa: BLE001
                logger.warning("[App] failed to stop previous %sChannel: %s", channel_name.capitalize(), e)
            channel_manager.unregister_channel(channel.channel_id)

    def _is_channel_enabled(conf: dict | None, required_fields: list[str]) -> tuple[bool, str]:
        if conf is None:
            return False, "missing or invalid config"
        enabled_raw = conf.get("enabled", None)
        if enabled_raw is None:
            all_fields_present = all(conf.get(f) for f in required_fields)
            return all_fields_present, f"missing {','.join(required_fields)}" if not all_fields_present else ""
        return bool(enabled_raw), "enabled = false" if not enabled_raw else ""

    async def _apply_channel_config(conf: dict) -> None:
        nonlocal feishu_channel, feishu_task, xiaoyi_channel, xiaoyi_task
        nonlocal dingtalk_channel, dingtalk_task, telegram_channel, telegram_task
        nonlocal discord_channel, discord_task
        nonlocal slack_channel, slack_task
        nonlocal whatsapp_channel, whatsapp_task
        nonlocal wecom_channel, wecom_task
        nonlocal wechat_channel, wechat_task
        nonlocal ssh_channel, ssh_task
        nonlocal _last_channels_conf
        nonlocal feishu_enterprise_channels, feishu_enterprise_tasks

        # === 新增：入口归一化（必须在 _should_restart_channel 之前） ===
        if isinstance(conf, dict):
            feishu_raw = conf.get("feishu")
            if isinstance(feishu_raw, dict):
                conf["feishu"] = _normalize_feishu_conf(feishu_raw)
            xiaoyi_raw = conf.get("xiaoyi")
            if isinstance(xiaoyi_raw, dict):
                conf["xiaoyi"] = _normalize_xiaoyi_conf(xiaoyi_raw)
        # ==========================================================

        restart_pending = channel_manager.pop_channel_restart_pending()
        changed_channels: list[str] = []
        for channel_name in [
            "feishu",
            "feishu_enterprise",
            "xiaoyi",
            "dingtalk",
            "telegram",
            "whatsapp",
            "discord",
            "slack",
            "wecom",
            "wechat",
            "ssh",
        ]:
            if _should_restart_channel(channel_name, _last_channels_conf, conf) or channel_name in restart_pending:
                if channel_name in restart_pending and not _should_restart_channel(
                        channel_name, _last_channels_conf, conf
                ):
                    logger.info(
                        "[App] channels.%s force restart requested; cached runtime state must be dropped",
                        channel_name,
                    )
                changed_channels.append(channel_name)
        _last_channels_conf = dict(conf or {})

        if "feishu" in changed_channels:
            feishu_conf = conf.get("feishu") if isinstance(conf, dict) else {}

            # ---- 停止旧 apps 实例（从 channel_manager 查找） ----
            # 先 pop 再停止，避免 _stop_channel 内部 unregister_channel(channel_id)
            # 批量删除后后续 key 访问抛 KeyError
            for ch in channel_manager.pop_channels_by_id("feishu"):
                await _stop_channel(ch, getattr(ch, "start_task", None), f"feishu_app[{ch.app_id}]")
            # 统一注销 adapter（所有 feishu app 共享 "feishu" 标识）
            im_inbound.unregister_adapter("feishu")
            im_outbound.unregister_adapter("feishu")

            # 单变量置空（不再使用，但保持 nonlocal 兼容）
            feishu_channel, feishu_task = None, None

            apps = feishu_conf.get("apps") or []
            if not apps:
                logger.info("[App] channels.feishu.apps empty, FeishuChannel disabled")
            else:
                for app in apps:
                    if not app.get("enabled", True):
                        continue
                    enabled, reason = _is_channel_enabled(app, ["app_id", "app_secret"])
                    if not enabled:
                        logger.info("[App] channels.feishu.apps[].%s, skipping", reason)
                        continue

                    app_id = str(app.get("app_id") or "").strip()
                    channel_id = "feishu"

                    feishu_config = FeishuConfig(
                        enabled=True,
                        app_id=app_id,
                        app_secret=str(app.get("app_secret") or "").strip(),
                        encrypt_key=str(app.get("encrypt_key") or "").strip(),
                        verification_token=str(app.get("verification_token") or "").strip(),
                        allow_from=app.get("allow_from") or [],
                        enable_streaming=bool(app.get("enable_streaming", True)),
                        chat_id=str(app.get("chat_id") or "").strip(),
                        channel_id=channel_id,
                        last_chat_id=str(app.get("last_chat_id") or "").strip(),
                        last_open_id=str(app.get("last_open_id") or "").strip(),
                        group_digital_avatar=bool(app.get("group_digital_avatar", False)),
                        my_user_id=str(app.get("my_user_id") or app.get("my_open_id") or "").strip(),
                        bot_name=str(app.get("bot_name") or "").strip(),
                        enable_memory=bool(app.get("enable_memory", False)),
                        message_merge_window_ms=int(app.get("message_merge_window_ms", 15000)),
                        api_base=str(app.get("api_base") or "").strip() or _FEISHU_DEFAULT_API_BASE,
                    )
                    # 数字分身 adapter（共享 "feishu" key）
                    feishu_adapter = None
                    if feishu_config.group_digital_avatar:
                        from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_im_adapter import \
                            FeishuIMPlatformAdapter
                        feishu_adapter = FeishuIMPlatformAdapter(
                            my_open_id=feishu_config.my_user_id,
                            bot_name=feishu_config.bot_name,
                        )
                        im_inbound.register_adapter(channel_id, feishu_adapter)
                        im_outbound.register_adapter(channel_id, feishu_adapter)

                    channel = FeishuChannel(feishu_config, _DummyBus(), im_platform_adapter=feishu_adapter)
                    channel_manager.register_channel(channel)
                    task = asyncio.create_task(channel.start(), name=f"feishu-{app_id}")
                    channel.start_task = task  # 挂到 channel 对象上，不另存 dict
                    logger.info("[App] FeishuChannel(app=%s) registered from channels.feishu.apps", app_id)

        if "feishu_enterprise" in changed_channels:
            for bot_key, task in list(feishu_enterprise_tasks.items()):
                await _stop_channel(
                    feishu_enterprise_channels.get(bot_key),
                    task,
                    f"feishu_enterprise[{bot_key}]",
                )
            for _old_ch in feishu_enterprise_channels.values():
                _old_ch_id = getattr(_old_ch, "_channel_id", "") or getattr(_old_ch, "name", "")
                if _old_ch_id:
                    im_inbound.unregister_adapter(_old_ch_id)
                    im_outbound.unregister_adapter(_old_ch_id)
            feishu_enterprise_channels = {}
            feishu_enterprise_tasks = {}

            enterprise_conf = conf.get("feishu_enterprise") if isinstance(conf, dict) else None
            if not isinstance(enterprise_conf, dict):
                logger.info(
                    "[App] channels.feishu_enterprise missing or invalid; "
                    "FeishuEnterpriseChannel disabled"
                )
            else:
                for bot_key, bot_conf_raw in enterprise_conf.items():
                    if not isinstance(bot_key, str) or not bot_key.strip():
                        continue
                    bot_conf = bot_conf_raw if isinstance(bot_conf_raw, dict) else None
                    if bot_conf is None:
                        logger.info("[App] channels.feishu_enterprise.%s invalid config, skipping", bot_key)
                        continue
                    enabled, reason = _is_channel_enabled(bot_conf, ["app_id", "app_secret"])
                    if not enabled:
                        logger.info(
                            "[App] channels.feishu_enterprise.%s.%s, FeishuEnterpriseChannel disabled",
                            bot_key,
                            reason,
                        )
                        continue

                    bot_key = bot_key.strip()
                    app_id = str(bot_conf.get("app_id") or "").strip()
                    channel_id = f"feishu_enterprise:{app_id}"
                    feishu_config = FeishuConfig(
                        enabled=True,
                        app_id=app_id,
                        app_secret=str(bot_conf.get("app_secret") or "").strip(),
                        encrypt_key=str(bot_conf.get("encrypt_key") or "").strip(),
                        verification_token=str(bot_conf.get("verification_token") or "").strip(),
                        allow_from=bot_conf.get("allow_from") or [],
                        enable_streaming=bool(bot_conf.get("enable_streaming", True)),
                        chat_id=str(bot_conf.get("chat_id") or "").strip(),
                        channel_id=channel_id,
                        bot_key=bot_key,
                        last_chat_id=str(bot_conf.get("last_chat_id") or "").strip(),
                        last_open_id=str(bot_conf.get("last_open_id") or "").strip(),
                        my_user_id=str(bot_conf.get("my_user_id") or "").strip(),
                        bot_name=str(bot_conf.get("bot_name") or "").strip(),
                        group_digital_avatar=bool(bot_conf.get("group_digital_avatar", False)),
                        enable_memory=bool(bot_conf.get("enable_memory", False)),
                        api_base=str(bot_conf.get("api_base") or "").strip() or _FEISHU_DEFAULT_API_BASE,
                    )
                    feishu_adapter = None
                    if feishu_config.group_digital_avatar:
                        from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_im_adapter import \
                            FeishuIMPlatformAdapter
                        feishu_adapter = FeishuIMPlatformAdapter(
                            my_open_id=feishu_config.my_user_id,
                            bot_name=feishu_config.bot_name,
                        )
                        im_inbound.register_adapter(channel_id, feishu_adapter)
                        im_outbound.register_adapter(channel_id, feishu_adapter)
                    channel = FeishuChannel(feishu_config, _DummyBus(), im_platform_adapter=feishu_adapter)
                    channel_manager.register_channel(channel)
                    task = asyncio.create_task(channel.start(), name=f"feishu-enterprise-{bot_key}")
                    feishu_enterprise_channels[bot_key] = channel
                    feishu_enterprise_tasks[bot_key] = task
                    logger.info(
                        "[App] registered FeishuChannel(%s) from config.yaml.channels.feishu_enterprise.%s",
                        bot_key,
                        channel_id,
                    )

        if "xiaoyi" in changed_channels:
            xiaoyi_conf = conf.get("xiaoyi") if isinstance(conf, dict) else {}

            # ---- 停止旧 apps 实例（从 channel_manager 查找） ----
            # 先 pop 再停止，避免 _stop_channel 内部 unregister_channel(channel_id)
            # 批量删除后后续 key 访问抛 KeyError
            for ch in channel_manager.pop_channels_by_id("xiaoyi"):
                await _stop_channel(ch, getattr(ch, "start_task", None), f"xiaoyi_app[{ch.app_id}]")

            # 单变量置空（不再使用，但保持 nonlocal 兼容）
            xiaoyi_channel, xiaoyi_task = None, None

            apps = xiaoyi_conf.get("apps") or []
            if not apps:
                logger.info("[App] channels.xiaoyi.apps empty, XiaoyiChannel disabled")
            else:
                for app in apps:
                    if not app.get("enabled", True):
                        continue
                    enabled, reason = _is_channel_enabled(app, ["ak", "sk", "agent_id"])
                    if not enabled:
                        logger.info("[App] channels.xiaoyi.apps[].%s, skipping", reason)
                        continue

                    api_id = str(app.get("api_id") or "").strip()
                    is_default = app.get("is_default", False) or len(apps) == 1
                    if not api_id and not is_default:
                        logger.warning(
                            "[App] channels.xiaoyi.apps[].api_id required for non-default, skipping"
                        )
                        continue

                    channel_id = "xiaoyi"
                    config = XiaoyiChannelConfig(
                        enabled=True,
                        channel_id=channel_id,
                        mode=str(app.get("mode") or "xiaoyi_channel").strip(),
                        ak=str(app.get("ak") or "").strip(),
                        sk=str(app.get("sk") or "").strip(),
                        agent_id=str(app.get("agent_id") or "").strip(),
                        api_id=api_id,
                        enable_streaming=bool(app.get("enable_streaming", True)),
                        # 以下字段 _normalize_xiaoyi_conf 会自动填充缺省值，
                        # 此处显式读取以兼容直接使用 apps 格式的场景
                        ws_url1=str(app.get("ws_url1") or "").strip(),
                        ws_url2=str(app.get("ws_url2") or "").strip(),
                        uid=str(app.get("uid") or "").strip(),
                        api_key=str(app.get("api_key") or "").strip(),
                        push_id=str(app.get("push_id") or "").strip(),
                        push_url=str(app.get("push_url") or "").strip() or _XIAOYI_DEFAULT_PUSH_URL,
                        file_upload_url=str(app.get("file_upload_url") or "").strip(),
                    )
                    channel = XiaoyiChannel(config, _DummyBus())
                    channel_manager.register_channel(channel)
                    task = asyncio.create_task(channel.start(), name=f"xiaoyi-{api_id or 'default'}")
                    channel.start_task = task  # 挂到 channel 对象上，不另存 dict
                    logger.info("[App] XiaoyiChannel(api_id=%s) from channels.xiaoyi.apps", api_id or "default")

        if "dingtalk" in changed_channels:
            dingtalk_conf = conf.get("dingtalk") if isinstance(conf, dict) else None
            await _stop_channel(dingtalk_channel, dingtalk_task, "dingtalk", background_wait=True)
            dingtalk_channel, dingtalk_task = None, None

            if isinstance(dingtalk_conf, dict):
                enabled, reason = _is_channel_enabled(dingtalk_conf, ["client_id", "client_secret"])
                if not enabled:
                    logger.info("[App] channels.dingtalk.%s, DingTalkChannel disabled", reason)
                else:
                    dingtalk_config = DingTalkConfig(
                        enabled=True,
                        client_id=str(dingtalk_conf.get("client_id") or "").strip(),
                        client_secret=str(dingtalk_conf.get("client_secret") or "").strip(),
                        allow_from=dingtalk_conf.get("allow_from") or [],
                        api_base=str(dingtalk_conf.get("api_base") or "").strip() or _DINGTALK_DEFAULT_API_BASE,
                        oapi_base=str(dingtalk_conf.get("oapi_base") or "").strip() or _DINGTALK_DEFAULT_OAPI_BASE,
                    )
                    dingtalk_channel = DingTalkChannel(dingtalk_config, _DummyBus())
                    channel_manager.register_channel(dingtalk_channel)
                    dingtalk_task = asyncio.create_task(dingtalk_channel.start(), name="dingtalk")
                    logger.info("[App] DingTalkChannel registered from config.yaml.channels.dingtalk")
            else:
                logger.info("[App] channels.dingtalk missing or invalid, DingTalkChannel disabled")

        if "telegram" in changed_channels:
            telegram_conf = conf.get("telegram") if isinstance(conf, dict) else None
            await _stop_channel(telegram_channel, telegram_task, "telegram")
            telegram_channel, telegram_task = None, None

            if isinstance(telegram_conf, dict):
                enabled, reason = _is_channel_enabled(telegram_conf, ["bot_token"])
                if not enabled:
                    logger.info("[App] channels.telegram.%s, TelegramChannel disabled", reason)
                else:
                    telegram_config = TelegramChannelConfig(
                        enabled=True,
                        bot_token=str(telegram_conf.get("bot_token") or "").strip(),
                        allow_from=telegram_conf.get("allow_from") or [],
                        parse_mode=str(telegram_conf.get("parse_mode") or "Markdown").strip(),
                        group_chat_mode=str(telegram_conf.get("group_chat_mode") or "mention").strip(),
                    )
                    telegram_channel = TelegramChannel(telegram_config, _DummyBus())
                    channel_manager.register_channel(telegram_channel)
                    telegram_task = asyncio.create_task(telegram_channel.start(), name="telegram")
                    logger.info("[App] TelegramChannel registered from config.yaml.channels.telegram")
            else:
                logger.info("[App] channels.telegram missing or invalid, TelegramChannel disabled")

        if "discord" in changed_channels:
            discord_conf = conf.get("discord") if isinstance(conf, dict) else None
            await _stop_channel(discord_channel, discord_task, "discord")
            discord_channel, discord_task = None, None

            if isinstance(discord_conf, dict):
                enabled, reason = _is_channel_enabled(discord_conf, ["bot_token"])
                if not enabled:
                    logger.info("[App] channels.discord.%s, DiscordChannel disabled", reason)
                else:
                    discord_config = DiscordChannelConfig(
                        enabled=True,
                        bot_token=str(discord_conf.get("bot_token") or "").strip(),
                        application_id=str(discord_conf.get("application_id") or "").strip(),
                        guild_id=str(discord_conf.get("guild_id") or "").strip(),
                        channel_id=str(discord_conf.get("channel_id") or "").strip(),
                        allow_from=discord_conf.get("allow_from") or [],
                        block_dm=(str(discord_conf.get("block_dm")).lower() in ["true", "1"]) or False,
                    )
                    discord_channel = DiscordChannel(discord_config, _DummyBus())
                    channel_manager.register_channel(discord_channel)
                    discord_task = asyncio.create_task(discord_channel.start(), name="discord")
                    logger.info("[App] DiscordChannel registered from config.yaml.channels.discord")
            else:
                logger.info("[App] channels.discord missing or invalid, DiscordChannel disabled")

        if "slack" in changed_channels:
            slack_conf = conf.get("slack") if isinstance(conf, dict) else None
            await _stop_channel(slack_channel, slack_task, "slack")
            slack_channel, slack_task = None, None

            if isinstance(slack_conf, dict):
                enabled, reason = _is_channel_enabled(slack_conf, ["bot_token", "app_token"])
                if not enabled:
                    logger.info("[App] channels.slack.%s, SlackChannel disabled", reason)
                else:
                    reply_in_thread_raw = slack_conf.get("reply_in_thread", True)
                    reply_in_thread = (
                        str(reply_in_thread_raw).strip().lower() in ("true", "1", "yes", "on")
                        if isinstance(reply_in_thread_raw, str)
                        else bool(reply_in_thread_raw)
                    )
                    slack_config = SlackChannelConfig(
                        enabled=True,
                        bot_token=str(slack_conf.get("bot_token") or "").strip(),
                        app_token=str(slack_conf.get("app_token") or "").strip(),
                        allow_from=slack_conf.get("allow_from") or [],
                        allowed_channel_ids=slack_conf.get("allowed_channel_ids") or [],
                        default_channel_id=str(slack_conf.get("default_channel_id") or "").strip(),
                        reply_in_thread=reply_in_thread,
                    )
                    slack_channel = SlackChannel(slack_config, _DummyBus())
                    channel_manager.register_channel(slack_channel)
                    slack_task = asyncio.create_task(slack_channel.start(), name="slack")
                    logger.info("[App] SlackChannel registered from config.yaml.channels.slack")
            else:
                logger.info("[App] channels.slack missing or invalid, SlackChannel disabled")

        if "whatsapp" in changed_channels:
            whatsapp_conf = conf.get("whatsapp") if isinstance(conf, dict) else None
            await _stop_channel(whatsapp_channel, whatsapp_task, "whatsapp")
            whatsapp_channel, whatsapp_task = None, None

            if isinstance(whatsapp_conf, dict):
                bridge_ws_url = str(whatsapp_conf.get("bridge_ws_url") or "ws://127.0.0.1:19600/ws").strip()
                default_jid = str(whatsapp_conf.get("default_jid") or "").strip()
                allow_from = whatsapp_conf.get("allow_from") or []
                enable_streaming = bool(whatsapp_conf.get("enable_streaming", True))
                auto_start_bridge = bool(whatsapp_conf.get("auto_start_bridge", False))
                bridge_command = str(
                    whatsapp_conf.get("bridge_command") or "node scripts/whatsapp-bridge.js"
                ).strip()
                bridge_workdir = str(whatsapp_conf.get("bridge_workdir") or "").strip()
                bridge_env_raw = whatsapp_conf.get("bridge_env") or {}
                bridge_env = bridge_env_raw if isinstance(bridge_env_raw, dict) else {}

                enabled_raw = whatsapp_conf.get("enabled", None)
                if enabled_raw is None:
                    enabled = bool(bridge_ws_url)
                else:
                    enabled = bool(enabled_raw)

                if not enabled:
                    logger.info("[App] channels.whatsapp.enabled = false, WhatsAppChannel disabled")
                elif not bridge_ws_url:
                    logger.info("[App] channels.whatsapp missing bridge_ws_url, WhatsAppChannel disabled")
                else:
                    whatsapp_config = WhatsAppChannelConfig(
                        enabled=True,
                        enable_streaming=enable_streaming,
                        bridge_ws_url=bridge_ws_url,
                        allow_from=allow_from,
                        default_jid=default_jid,
                        auto_start_bridge=auto_start_bridge,
                        bridge_command=bridge_command,
                        bridge_workdir=bridge_workdir,
                        bridge_env={str(k): str(v) for k, v in bridge_env.items()},
                    )
                    whatsapp_channel = WhatsAppChannel(whatsapp_config, _DummyBus())
                    channel_manager.register_channel(whatsapp_channel)
                    whatsapp_task = asyncio.create_task(whatsapp_channel.start(), name="whatsapp")
                    logger.info("[App] WhatsAppChannel registered from config.yaml.channels.whatsapp")
            else:
                logger.info("[App] channels.whatsapp missing or invalid, WhatsAppChannel disabled")

        if "wecom" in changed_channels:
            wecom_conf = conf.get("wecom") if isinstance(conf, dict) else None
            await _stop_channel(wecom_channel, wecom_task, "wecom")
            wecom_channel, wecom_task = None, None

            if isinstance(wecom_conf, dict):
                enabled, reason = _is_channel_enabled(wecom_conf, ["bot_id", "secret"])
                if not enabled:
                    logger.info("[App] channels.wecom.%s, WecomChannel disabled", reason)
                else:
                    wecom_config = WecomConfig(
                        enabled=True,
                        bot_id=str(wecom_conf.get("bot_id") or "").strip(),
                        secret=str(wecom_conf.get("secret") or "").strip(),
                        ws_url=str(wecom_conf.get("ws_url") or "wss://openws.work.weixin.qq.com").strip(),
                        allow_from=wecom_conf.get("allow_from") or [],
                        enable_streaming=bool(wecom_conf.get("enable_streaming", True)),
                        send_thinking_message=bool(wecom_conf.get("send_thinking_message", True)),
                        group_digital_avatar=bool(wecom_conf.get("group_digital_avatar", False)),
                        my_user_id=str(wecom_conf.get("my_user_id") or "").strip(),
                        bot_name=str(wecom_conf.get("bot_name") or "").strip(),
                        enable_memory=bool(wecom_conf.get("enable_memory", False)),
                    )
                    # 数字分身：创建 adapter 并注册到 pipeline
                    wecom_adapter = None
                    if wecom_config.group_digital_avatar:
                        from jiuwenswarm.gateway.channel_manager.im_platforms.wecom.wecom_im_adapter import \
                            WecomIMPlatformAdapter
                        wecom_adapter = WecomIMPlatformAdapter(
                            my_user_id=wecom_config.my_user_id,
                            bot_name=wecom_config.bot_name,
                        )
                        im_inbound.register_adapter("wecom", wecom_adapter)
                        im_outbound.register_adapter("wecom", wecom_adapter)
                    wecom_channel = WecomChannel(wecom_config, _DummyBus(), im_platform_adapter=wecom_adapter)
                    channel_manager.register_channel(wecom_channel)
                    wecom_task = asyncio.create_task(wecom_channel.start(), name="wecom")
                    logger.info("[App] WecomChannel registered from config.yaml.channels.wecom")
            else:
                logger.info("[App] channels.wecom missing or invalid, WecomChannel disabled")

        if "wechat" in changed_channels:
            wechat_conf = conf.get("wechat") if isinstance(conf, dict) else None
            await _stop_channel(wechat_channel, wechat_task, "wechat")
            wechat_channel, wechat_task = None, None

            if isinstance(wechat_conf, dict):
                enabled, reason = _is_channel_enabled(wechat_conf, [])
                if not enabled:
                    logger.info("[App] channels.wechat.%s, WechatChannel disabled", reason)
                else:
                    wechat_config = WechatConfig(
                        enabled=True,
                        base_url=str(wechat_conf.get("base_url") or "https://ilinkai.weixin.qq.com").strip(),
                        bot_token=str(wechat_conf.get("bot_token") or "").strip(),
                        ilink_bot_id=str(wechat_conf.get("ilink_bot_id") or "").strip(),
                        ilink_user_id=str(wechat_conf.get("ilink_user_id") or "").strip(),
                        allow_from=wechat_conf.get("allow_from") or [],
                        auto_login=bool(wechat_conf.get("auto_login", True)),
                        qrcode_poll_interval_sec=float(wechat_conf.get("qrcode_poll_interval_sec", 2.0)),
                        long_poll_timeout_sec=int(wechat_conf.get("long_poll_timeout_sec", 45)),
                        backoff_base_sec=float(wechat_conf.get("backoff_base_sec", 1.0)),
                        backoff_max_sec=float(wechat_conf.get("backoff_max_sec", 30.0)),
                        credential_file=str(
                            wechat_conf.get("credential_file") or "~/.wx-ai-bridge/credentials.json"
                        ).strip(),
                        enable_streaming=bool(wechat_conf.get("enable_streaming", True)),
                    )
                    wechat_channel = WechatChannel(wechat_config, _DummyBus())
                    channel_manager.register_channel(wechat_channel)
                    wechat_task = asyncio.create_task(wechat_channel.start(), name="wechat")
                    logger.info("[App] WechatChannel registered from config.yaml.channels.wechat")
            else:
                logger.info("[App] channels.wechat missing or invalid, WechatChannel disabled")

        if "ssh" in changed_channels:
            ssh_conf = conf.get("ssh") if isinstance(conf, dict) else None
            await _stop_channel(ssh_channel, ssh_task, "ssh")
            ssh_channel, ssh_task = None, None
            # Clear issuer whenever SSH channel is torn down / reconfigured.
            _set_agentos_ssh_key_issuer(None)

            if isinstance(ssh_conf, dict):
                # 南向经 agent client（如 agentos_router -> yuanrong）动态解析。
                enabled, reason = _is_channel_enabled(ssh_conf, ["listen_port"])
                full_cfg = get_config()
                gateway_cfg = full_cfg.get("gateway") if isinstance(full_cfg, dict) else {}
                agent_client_cfg = (
                    gateway_cfg.get("agent_client") if isinstance(gateway_cfg, dict) else {}
                )
                client_type = (
                    str(agent_client_cfg.get("type") or "websocket").strip().lower()
                    if isinstance(agent_client_cfg, dict)
                    else "websocket"
                )
                if not enabled:
                    logger.info("[App] channels.ssh.%s, SshChannel disabled", reason)
                elif client_type != "agentos_router":
                    logger.warning(
                        "[App] channels.ssh.enabled=true but gateway.agent_client.type=%s "
                        "(require agentos_router); SshChannel will not start",
                        client_type,
                    )
                else:
                    ssh_config = SshChannelConfig.from_dict({**ssh_conf, "enabled": True})
                    ssh_channel = SshChannel(
                        ssh_config,
                        _DummyBus(),
                        key_registry=KeyRegistry(),
                    )
                    channel_manager.register_channel(ssh_channel)
                    ssh_task = asyncio.create_task(ssh_channel.start(), name="ssh")

                    def _on_ssh_task_done(task: asyncio.Task) -> None:
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            return
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                "[App] SSH channel failed to start: %s. "
                                "If SSH is enabled, install optional dependency with "
                                "`uv sync --extra ssh` or `pip install \"jiuwenswarm[ssh]\"`.",
                                exc,
                            )

                    ssh_task.add_done_callback(_on_ssh_task_done)
                    if ssh_config.auth.enabled:
                        _set_agentos_ssh_key_issuer(
                            ssh_channel.key_issuer,
                            ephemeral_key_ttl_sec=ssh_config.auth.ephemeral_key_ttl_sec,
                        )
                    logger.info(
                        "[App] SshChannel registered from config.yaml.channels.ssh "
                        "(listen %s:%s -> MessageHandler; southbound via agent client; auth=%s)",
                        ssh_config.listen_host,
                        ssh_config.listen_port,
                        ssh_config.auth.enabled,
                    )
            else:
                logger.info("[App] channels.ssh missing or invalid, SshChannel disabled")

        _schedule_agent_prewarm_sync(
            "agent-prewarm-sync-after-channel-change",
            delay_seconds=1.0,
        )

    channel_manager.set_config_callback(_apply_channel_config)
    await channel_manager.set_config(initial_channels_conf)
    _schedule_agent_prewarm_sync(
        "agent-prewarm-sync-after-startup",
        delay_seconds=3.0,
    )
    prewarm_sync_task = asyncio.create_task(
        _periodic_agent_prewarm_sync(),
        name="agent-prewarm-periodic-sync",
    )

    # ---------- Opencode Zen 免费模型预热 ----------
    # Gateway 进程独立拉取 Zen 免费模型到内存缓存（_models_list 在此进程处理，
    # 与 AgentServer 内存不共享，故各自预热）。失败留空，不阻断启动。
    try:
        from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models
        await warm_zen_free_models(reason="gateway-startup")
    except Exception as e:  # noqa: BLE001 - 兜底
        logger.warning("[App] zen free models warm failed (non-fatal): %s", e)

    await channel_manager.start_dispatch()
    # cron jobs 的 work_mode 补全已改为惰性迁移:scheduler.start() → reload() →
    # list_jobs() 读取时按需推断并写回磁盘(见 CronJobStore.list_jobs),无需启动全量扫描。
    await cron_scheduler.start()
    # 主动推荐：按 config 自动注册/删除 proactive.tick 定时 job
    try:
        from jiuwenswarm.gateway.cron.proactive_cron_sync import sync_proactive_tick_job
        await sync_proactive_tick_job(cron_controller, get_config())
    except Exception as e:  # noqa: BLE001  # 兜底：启动时 proactive job 注册失败不阻断 Gateway 启动
        logger.warning("[App] proactive.tick auto-register failed (non-fatal): %s", e)
    # 先同步完成监听绑定，避免 IDE/ACP 子进程在端口尚未就绪时连接导致多次重试。
    await gateway_server.start()
    gateway_server_task = asyncio.create_task(
        gateway_server.wait_until_closed(),
        name="acp-gateway-server",
    )
    web_task = (
        asyncio.create_task(web_channel.start(), name="web-channel")
        if web_channel is not None
        else None
    )
    if web_channel is not None:
        logger.info(
            "[App] started: Web ws://%s:%s%s  AgentServer: %s  Press Ctrl+C to exit.",
            web_host,
            web_port,
            web_path,
            agent_server_url,
        )

    restart_requested = False
    try:
        tasks_to_wait = [task for task in (gateway_server_task, web_task) if task is not None]
        if tasks_to_wait:
            restart_requested = await _wait_for_gateway_tasks_or_restart(
                tasks_to_wait,
                restart_request,
            )
    except KeyboardInterrupt:
        logger.info("received Ctrl+C, shutting down...")
    except asyncio.CancelledError:
        pass
    finally:
        if prewarm_sync_debounce_task is not None:
            prewarm_sync_debounce_task.cancel()
            try:
                await prewarm_sync_debounce_task
            except asyncio.CancelledError:
                pass
        prewarm_sync_task.cancel()
        try:
            await prewarm_sync_task
        except asyncio.CancelledError:
            pass
        if a2a_task is not None:
            a2a_task.cancel()
            try:
                await a2a_task
            except asyncio.CancelledError:
                pass
        await a2a_channel.stop()
        channel_manager.unregister_channel(a2a_channel.channel_id)
        if gateway_server_task is not None:
            gateway_server_task.cancel()
            try:
                await gateway_server_task
            except asyncio.CancelledError:
                pass
        await gateway_server.stop()
        await acp_inbound_server.stop()
        if tui_channel is not None:
            await tui_channel.stop()
        if web_task is not None:
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        if web_channel is not None:
            await web_channel.stop()

        if feishu_channel is not None and feishu_task is not None:
            feishu_task.cancel()
            try:
                await feishu_task
            except asyncio.CancelledError:
                pass
            await feishu_channel.stop()
        for bot_key, task in list(feishu_enterprise_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            channel = feishu_enterprise_channels.get(bot_key)
            if channel is not None:
                await channel.stop()
        if xiaoyi_channel is not None and xiaoyi_task is not None:
            xiaoyi_task.cancel()
            try:
                await xiaoyi_task
            except asyncio.CancelledError:
                pass
            await xiaoyi_channel.stop()
        # ---- 从 channel_manager 清理所有动态注册的 channel 实例 ----
        for _cid in ("feishu", "xiaoyi"):
            for ch in channel_manager.pop_channels_by_id(_cid):
                task = getattr(ch, "start_task", None)
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                await ch.stop()
        # -----------------------------------
        if dingtalk_channel is not None and dingtalk_task is not None:
            dingtalk_task.cancel()
            try:
                await dingtalk_task
            except (TypeError, asyncio.CancelledError):
                pass
            await dingtalk_channel.stop()
        if telegram_channel is not None and telegram_task is not None:
            telegram_task.cancel()
            try:
                await telegram_task
            except asyncio.CancelledError:
                pass
            await telegram_channel.stop()
        if discord_channel is not None and discord_task is not None:
            discord_task.cancel()
            try:
                await discord_task
            except asyncio.CancelledError:
                pass
            await discord_channel.stop()
        if slack_channel is not None and slack_task is not None:
            slack_task.cancel()
            try:
                await slack_task
            except asyncio.CancelledError:
                pass
            await slack_channel.stop()
        if whatsapp_channel is not None and whatsapp_task is not None:
            whatsapp_task.cancel()
            try:
                await whatsapp_task
            except asyncio.CancelledError:
                pass
            await whatsapp_channel.stop()
        if wecom_channel is not None and wecom_task is not None:
            wecom_task.cancel()
            try:
                await wecom_task
            except asyncio.CancelledError:
                pass
            await wecom_channel.stop()
        if wechat_channel is not None and wechat_task is not None:
            wechat_task.cancel()
            try:
                await wechat_task
            except asyncio.CancelledError:
                pass
            await wechat_channel.stop()
        if ssh_channel is not None and ssh_task is not None:
            ssh_task.cancel()
            try:
                await ssh_task
            except asyncio.CancelledError:
                pass
            await ssh_channel.stop()
            _set_agentos_ssh_key_issuer(None)

        await cron_scheduler.stop()
        await channel_manager.stop_dispatch()
        await heartbeat_service.stop()
        await message_handler.stop_forwarding()
        await client.disconnect()

        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except (asyncio.CancelledError, Exception):
            pass

        logger.info("[App] Gateway stopped")

    if restart_requested:
        _exec_gateway_restart()


def main() -> None:
    from jiuwenswarm.dotenv_early import get_parsed_dotenv

    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-gateway",
        description="Start JiuwenSwarm Gateway + Channels (split deployment; connects to jiuwenswarm-agentserver).",
    )
    parser.add_argument(
        "--agent-server-url",
        "-u",
        default=None,
        metavar="URL",
        help="AgentServer WebSocket URL (default: AGENT_SERVER_URL or ws://AGENT_SERVER_HOST:AGENT_SERVER_PORT).",
    )
    parser.add_argument(
        "--host",
        "-H",
        default=None,
        metavar="HOST",
        help="WebChannel bind host (default: WEB_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="WebChannel bind port (default: WEB_PORT or 19000).",
    )
    parser.add_argument(
        "--web-path",
        default=None,
        metavar="PATH",
        help="WebChannel ws path (default: WEB_PATH or /ws).",
    )
    parser.add_argument(
        "--name",
        metavar="<name>",
        help="Start a named instance from instances.yaml.",
    )
    parser.add_argument(
        "--dotenv",
        metavar="<path>",
        help="Load environment from .env file (processed at startup, not used here).",
    )
    args = parser.parse_args()

    # Handle --name: check if bootstrap .env was loaded successfully
    # (parse_dotenv_early() already processed it, this is just a fallback check)
    if args.name:
        if get_parsed_dotenv() is None:
            # Early parsing failed - instance not found or workspace missing
            # Error was already printed by parse_dotenv_early()
            raise SystemExit(1)

    default_host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1")
    default_port = os.getenv("AGENT_SERVER_PORT") or os.getenv("AGENT_PORT", "18092")
    agent_server_url = (
            args.agent_server_url
            or os.getenv("AGENT_SERVER_URL")
            or f"ws://{default_host}:{default_port}"
    )
    web_host = args.host or os.getenv("WEB_HOST", "127.0.0.1")
    web_port = args.port or int(os.getenv("WEB_PORT", "19000"))
    web_path = args.web_path or os.getenv("WEB_PATH", "/ws")
    _dual_raw = os.getenv("WEB_DUAL_PROTOCOL", "1").strip().lower()
    web_dual_protocol = _dual_raw not in {"0", "false", "no", "off"}

    install_async_dump_handler("gateway")
    asyncio.run(
        _run(
            agent_server_url=agent_server_url,
            web_host=web_host,
            web_port=web_port,
            web_path=web_path,
            web_dual_protocol=web_dual_protocol,
        )
    )


if __name__ == "__main__":
    main()
