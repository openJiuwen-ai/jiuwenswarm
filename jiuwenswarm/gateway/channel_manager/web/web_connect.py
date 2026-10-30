# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""WebChannel - WebSocket 通道实现.

提供可扩展的方法处理器注册机制 (`register_method`) 和连接钩子 (`on_connect`)，
使上层应用可以灵活控制每个 req method 的行为，而无需修改通道本身。
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import os
import secrets
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import aiohttp
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.gateway.channel_manager.base import ChannelMetadata, RobotMessageRouter
from jiuwenswarm.gateway.routing.base_ws_channel import BaseWsChannel
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from jiuwenswarm.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_origin_check_enabled,
    is_allowed_browser_origin,
)
from jiuwenswarm.common.schema.message import EventType, Message, Mode, ReqMethod
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)

logger = logging.getLogger(__name__)

_WEB_CONNECTION_USER_ID_ATTR = "_web_connection_user_id"

_HANDLER_BEFORE_CALLBACK_METHODS = frozenset({ReqMethod.CHAT_SEND.value})

_STREAM_COALESCE_EVENT_TYPES = frozenset({"chat.delta", "chat.reasoning"})
_STREAM_COALESCE_MAX_FRAMES = 32

_WEB_FULL_PAYLOAD_EVENT_TYPES = frozenset(
    {
        "connection.ack",
        "todo.updated",
        "chat.tool_call",
        "chat.tool_update",
        "chat.tool_result",
        "chat.processing_status",
        "chat.interrupt_result",
        "chat.evolution_status",
        "chat.error",
        "heartbeat.relay",
        "context.usage",
        "context.compression_state",
        "chat.ask_user_question",
        "chat.subtask_update",
        "chat.symphony_status",
        "chat.notice",
        "history.message",
        "chat.session_result",
        "chat.usage_metadata",
        "chat.usage_summary",
        "chat.file",
        "chat.retract",
        "security.alert",
        "goal.snapshot",
        "goal.updated",
        # Web 的 Plan 开关靠该事件在计划执行后自动复位，需要完整 payload
        # 才能拿到退出后应回到的 mode。
        "plan.mode_exited",
        "runtime.accepted",
        "execution.error",
        "proactive_recommendation",
    }
)

# ── 类型别名 ──────────────────────────────────────────────
# 方法处理器签名: (ws, req_id, params, session_id) -> None
MethodHandler = Callable[..., Awaitable[None]]
# 连接钩子签名: (ws) -> None | Awaitable[None]
ConnectHook = Callable[..., Any]

# ── 命名管道形态（桌面集成，docs/named-pipe-migration-design.md §5.4）──
# 管道连接首帧 auth 的等待超时（秒）
_PIPE_AUTH_TIMEOUT_SECONDS = 10.0
# 管道连接的 remote 标识（日志/路由兜底 user_id 用；管道无对端地址概念）
_PIPE_REMOTE_LABEL = "named-pipe:cron"


@dataclass(frozen=True)
class _MethodHandlerInvocation:
    ws: Any
    method: str
    req_id: str
    params: dict[str, Any]
    session_id: str
    handler: MethodHandler


@dataclass
class WebChannelConfig:
    """WebChannel 配置."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 19000
    path: str = "/ws"
    allow_from: list[str] = field(default_factory=list)


class _PipeClientAdapter:
    """命名管道连接的 ws 皮：让管道连接复用 WebChannel 的协议内核与出站 writer。

    与 websockets 连接的差异仅在 IO 层：
    - ``send(data)``：writer 把出站 dict 帧序列化为 str 后调本方法，这里 loads 回
      dict 再按长度前缀帧发出——管道对端 ``recv_frame`` 直接拿到 JSON 对象，
      与 WS 客户端 ``JSON.parse`` 后的形态一致。
    - ``closed`` / ``close()``：映射到 ``PipeStream``（close 的 code/reason
      参数仅作 WS 兼容占位，管道无关闭帧语义）。
    连接级属性（``_jiuwen_ws_id`` / ``_jiuwen_initial_sid`` / 连接 user_id）
    由协议内核 setattr 挂到本对象上，与 WS 连接同机制。
    """

    def __init__(self, stream: Any, *, remote: str = _PIPE_REMOTE_LABEL) -> None:
        self._stream = stream
        self.remote_address = remote

    @property
    def closed(self) -> bool:
        return bool(self._stream.closed)

    async def send(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        if isinstance(data, str):
            data = json.loads(data)
        await self._stream.send_frame(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._stream.close()


class WebChannel(BaseWsChannel):
    """Web 前端 WebSocket 通道.

    核心职责：
    1. 管理 WebSocket 连接生命周期
    2. 解析帧协议 (req / res / event)
    3. 将入站消息发布到 RobotMessageRouter
    4. 将方法路由委托给通过 `register_method` 注册的处理器
    5. V2: 基于 _clients_by_key[RoutingKey] 的 5 维精确路由
    """

    name = "web"
    channel_id = "web"

    def __init__(self, config: WebChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: WebChannelConfig = config
        self._server: Any = None
        # 桌面形态的命名管道 server（np_transport.PipeServer；非桌面形态恒 None）
        self._pipe_server: Any = None
        # 桌面形态（仅管道）下 start() 的常驻退出事件（stop() 置位）
        self._pipe_only_stop: asyncio.Event | None = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._method_handlers: dict[str, MethodHandler] = {}
        self._connect_hooks: list[ConnectHook] = []
        self._disconnect_hooks: list[ConnectHook] = []
        # ws -> set[session_id]: 追踪每个连接上活跃的 session
        self._ws_sessions: dict[int, set[str]] = {}
        # session_id -> is_processing: 由 chat.processing_status 事件维护,
        # 供 /ws/git 写操作(如 discard_turn_changes)查询 agent 是否正在执行。
        # 未跟踪的 session 默认返回 False(不忙碌)。
        self._session_busy: dict[str, bool] = {}
        # Git diff 监控注册表(设计文档阶段10):由 app_gateway 在启动期注入,
        # handler 通过 ``getattr(channel, "git_watcher_registry", None)`` 防御性读取。
        self.git_watcher_registry: Any = None

    @staticmethod
    def _coalescible_stream_frame(
        frame: Any,
    ) -> tuple[dict[str, Any], str] | None:
        """Return (decoded frame, content) for a merge-safe stream frame.

        帧已是 dict（入队时不预序列化），故无需 json.loads；返回 decoded 与
        content 供 _coalesce 直接在 dict 层合并，省去 str↔dict 往返。
        """
        if not isinstance(frame, dict) or frame.get("type") != "event":
            return None
        if frame.get("event") not in _STREAM_COALESCE_EVENT_TYPES:
            return None
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return None
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        return frame, content

    @staticmethod
    def _same_stream_identity(
        a: dict[str, Any],
        b: dict[str, Any],
    ) -> bool:
        """两帧除 payload.content 外是否同流（可合并）。

        逐键比对 payload 非 content 字段 + 外层 event 等键，避免构造 comparable
        dict 副本与整 dict 哈希比对的开销。
        """
        a_payload = a["payload"]
        b_payload = b["payload"]
        if a.get("event") != b.get("event"):
            return False
        if a.get("type") != b.get("type"):
            return False
        for key in set(a_payload) | set(b_payload):
            if key == "content":
                continue
            if a_payload.get(key) != b_payload.get(key):
                return False
        return True

    def _coalesce(
        self,
        first_frame: Any,
        queue: asyncio.Queue,
    ) -> list[Any]:
        """Merge only contiguous stream frames with identical non-content data."""
        parsed = self._coalescible_stream_frame(first_frame)
        if parsed is None:
            return [first_frame]

        decoded, merged_content = parsed
        merged_count = 1
        trailing: list[Any] = []

        while merged_count < _STREAM_COALESCE_MAX_FRAMES:
            try:
                candidate = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if candidate is None:
                trailing.append(None)
                break
            candidate_parsed = self._coalescible_stream_frame(candidate)
            if (
                candidate_parsed is None
                or not self._same_stream_identity(decoded, candidate_parsed[0])
            ):
                trailing.append(candidate)
                break
            merged_content += candidate_parsed[1]
            merged_count += 1

        if merged_count == 1:
            return [first_frame, *trailing]

        merged_payload = {**decoded["payload"], "content": merged_content}
        return [{**decoded, "payload": merged_payload}, *trailing]

    # ── 公共属性 ──────────────────────────────────────────

    # channel_id 属性由 name 提供，BaseWsChannel.channel_id 通过 __init_subclass__ 或直接赋值为 "web"

    @property
    def clients(self) -> set[Any]:
        """当前活跃的 WebSocket 客户端集合（从 _clients_by_key 推导，只读副本）."""
        result: set[Any] = set()
        for ws_list in self._clients_by_key.values():
            result.update(ws_list)
        return result

    # ── 扩展注册 API ──────────────────────────────────────

    def register_method(self, method: str, handler: MethodHandler) -> None:
        """注册 req method 处理器.

        handler 签名: ``async def handler(ws, req_id, params, session_id) -> None``
        handler 应通过 `send_response` / `send_event` 向客户端回复。
        """
        self._method_handlers[method] = handler

    def on_connect(self, callback: ConnectHook) -> None:
        """注册连接建立钩子，新客户端接入时依次调用."""
        self._connect_hooks.append(callback)

    def on_disconnect(self, callback: ConnectHook) -> None:
        """注册连接断开钩子，客户端断连时依次调用.

        callback 签名: ``async def callback(ws, session_ids: set[str]) -> None``
        """
        self._disconnect_hooks.append(callback)

    def on_message(self, callback: Callable[[Message], None]) -> None:
        """注册消息接收回调（替代默认的 router.publish_user_messages）。"""
        self._on_message_cb = callback

    def wrap_message_callback(
        self, wrapper: Callable[[Callable[[Message], Any] | None, Message], Any],
    ) -> None:
        """包装现有的消息回调。wrapper 接收 (original_callback, msg) 并返回处理结果。"""
        original = self._on_message_cb

        def wrapped(msg):
            return wrapper(original, msg)

        self._on_message_cb = wrapped

    # ── 帧发送 API（公开给处理器使用）─────────────────────

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
        """向指定客户端发送 ``res`` 帧."""
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
            self._enqueue_send(ws, frame)
        except Exception as e:
            if bool(getattr(ws, "closed", False)):
                logger.debug(
                    "WebChannel send_response skipped on closed websocket: %s",
                    format_ws_diagnostics(
                        {"id": req_id},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )
                return
            raise

    async def send_event(
            self,
            ws: Any,
            event: str,
            payload: dict[str, Any],
            *,
            seq: int | None = None,
            stream_id: str | None = None,
    ) -> None:
        """向指定客户端发送 ``event`` 帧."""
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        if seq is not None:
            frame["seq"] = seq
        if stream_id is not None:
            frame["stream_id"] = stream_id
        try:
            self._enqueue_send(ws, frame)
        except Exception as e:
            if bool(getattr(ws, "closed", False)):
                logger.debug(
                    "WebChannel send_event skipped on closed websocket: %s",
                    format_ws_diagnostics(
                        {"event": event, "seq": seq, "stream_id": stream_id},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )
                return
            raise

    @staticmethod
    def _extract_query_user_id(flat_query: dict[str, str]) -> str | None:
        uid = str(flat_query.get("user_id", "") or "").strip()
        return uid or None

    @staticmethod
    def _extract_ws_header_user_id(ws: Any) -> str | None:
        headers = (
            getattr(getattr(ws, "request", None), "headers", None)
            or getattr(ws, "request_headers", None)
        )
        raw = get_header_value(headers, "X-User-Id")
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    @classmethod
    def _resolve_connection_user_id(cls, flat_query: dict[str, str], ws: Any) -> str | None:
        connection_user_id = cls._extract_query_user_id(flat_query) or cls._extract_ws_header_user_id(ws)
        setattr(ws, _WEB_CONNECTION_USER_ID_ATTR, connection_user_id)
        return connection_user_id

    @staticmethod
    def _connection_user_id(ws: Any) -> str | None:
        """返回 Web 连接建立时缓存的 user_id（query 或 X-User-Id Header）。"""
        uid = getattr(ws, _WEB_CONNECTION_USER_ID_ATTR, None)
        if uid is None:
            return None
        text = str(uid).strip()
        return text or None

    @staticmethod
    def _routing_key_user_id(connection_user_id: str | None, remote: Any) -> str:
        if connection_user_id:
            return connection_user_id
        return str(remote or "unknown")

    @classmethod
    def _resolve_ws_identity(
        cls,
        ws: Any,
        flat_query: dict[str, str],
        remote: Any,
        *,
        route_type: str = "ws",
    ) -> tuple[str | None, str]:
        """解析 ws 连接身份,供 /ws 和 /ws/git 共用(设计文档 §5.3.7)。

        Args:
            route_type: ``"ws"`` 主路由或 ``"git"`` /ws/git 路由,仅用于日志区分。

        Returns:
            ``(connection_user_id, routing_key_user_id)``
        """
        connection_user_id = cls._resolve_connection_user_id(flat_query, ws)
        routing_key_user_id = cls._routing_key_user_id(connection_user_id, remote)
        return connection_user_id, routing_key_user_id

    async def _invoke_method_handler(
            self,
            invocation: _MethodHandlerInvocation,
    ) -> bool:
        kwargs: dict[str, Any] = {}
        if "user_id" in inspect.signature(invocation.handler).parameters:
            kwargs["user_id"] = self._connection_user_id(invocation.ws)
        try:
            await invocation.handler(
                invocation.ws,
                invocation.req_id,
                invocation.params,
                invocation.session_id,
                **kwargs,
            )
            return True
        except Exception as e:
            ws_closed = bool(getattr(invocation.ws, "closed", False))
            if ws_closed:
                logger.warning(
                    "WebChannel method handler aborted on closed websocket: %s",
                    format_ws_diagnostics(
                        {
                            "method": invocation.method,
                            "id": invocation.req_id,
                            "session_id": invocation.session_id,
                        },
                        describe_ws_peer(invocation.ws),
                        describe_ws_exception(e),
                    ),
                )
                return False

            logger.error(
                "WebChannel method handler error: %s",
                format_ws_diagnostics(
                    {
                        "method": invocation.method,
                        "id": invocation.req_id,
                        "session_id": invocation.session_id,
                    },
                    describe_ws_peer(invocation.ws),
                    describe_ws_exception(e),
                ),
            )
            try:
                await self.send_response(
                    invocation.ws, invocation.req_id, ok=False,
                    error=f"handler error: {e}", code="INTERNAL_ERROR",
                )
            except Exception as send_err:
                logger.warning(
                    "WebChannel failed to send handler error response ({}): {}",
                    invocation.method, send_err,
                )
            return False

    async def broadcast_event(
            self,
            event: str,
            payload: dict[str, Any],
            *,
            seq: int | None = None,
            stream_id: str | None = None,
            exclude_ws: Any = None,
    ) -> None:
        """向所有已连接客户端广播 ``event`` 帧.

        exclude_ws: 排除单个发起方 ws（如 config.changed 的保存发起方），
        避免发起方收到自身触发的广播而误弹「丢弃草稿」确认框。发起方靠
        保存响应的本地乐观合并自行刷新，无需这条广播。
        """
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        if seq is not None:
            frame["seq"] = seq
        if stream_id is not None:
            frame["stream_id"] = stream_id
        clients = self.clients
        if exclude_ws is not None:
            clients = {c for c in clients if c is not exclude_ws}
        await self._broadcast_to(frame, clients)

    async def _download_file(self, url: str) -> bytes | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        logger.warning("WebChannel 文件下载失败: %s, 状态码: %s", url, response.status)
                        return None
        except Exception as e:
            logger.warning("WebChannel 文件下载异常: %s, 错误: %s", url, e)
            return None

    async def _process_files(self, params: dict[str, Any]) -> dict[str, Any]:
        files = params.get("files")
        if not files or not isinstance(files, list):
            return params

        downloaded_files = []
        workspace_dir = str(get_agent_workspace_dir())

        for file_info in files:
            if not isinstance(file_info, dict):
                downloaded_files.append(file_info)
                continue

            file_url = file_info.get("url") or file_info.get("uri") or ""
            file_name = file_info.get("name") or file_info.get("filename") or "unknown_file"

            if file_url:
                file_content = await self._download_file(file_url)
                if file_content:
                    try:
                        os.makedirs(workspace_dir, exist_ok=True)
                        file_path = os.path.join(workspace_dir, file_name)
                        with open(file_path, "wb") as f:
                            f.write(file_content)
                        file_info["path"] = file_path
                    except Exception as e:
                        logger.warning("WebChannel 文件保存失败: %s", e)

            downloaded_files.append(file_info)

        params["files"] = downloaded_files
        return params

    # ── Channel 生命周期 ──────────────────────────────────

    async def start(self) -> None:
        """启动 WebSocket 服务并监听客户端连接.

        桌面集成形态（密钥包已下发，secrets_loaded()）：不监听 TCP——cron 通道
        由命名管道承载（零监听端口目标，docs/named-pipe-migration-design.md）；
        非桌面形态（无密钥包）行为不变。
        """
        if self._running:
            logger.warning("WebChannel 已在运行")
            return
        if not self.config.enabled:
            logger.warning("WebChannel 未启用（enabled=False）")
            return

        from jiuwenswarm.common.secrets_bootstrap import secrets_loaded

        if secrets_loaded():
            # 桌面形态：仅管道 server，不监听 TCP（WS 18590 不再存在）
            await self._maybe_start_pipe_server()
            self._running = True
            # start() 语义是「常驻直到 stop」：用事件保持，stop() 置位退出
            self._pipe_only_stop = asyncio.Event()
            logger.info("WebChannel 桌面形态：仅命名管道监听（TCP 不监听）")
            await self._pipe_only_stop.wait()
            return

        try:
            from websockets.legacy.server import serve as ws_serve
        except Exception:  # pragma: no cover
            import websockets

            ws_serve = websockets.serve

        from jiuwenswarm.common.ws_limits import WEB_WS_MAX_MESSAGE_BYTES

        self._server = await ws_serve(
            self._connection_handler,
            self.config.host,
            self.config.port,
            process_request=self._process_request,
            ping_interval=20,
            ping_timeout=60,
            max_size=WEB_WS_MAX_MESSAGE_BYTES,
        )
        # 桌面形态：并行起命名管道 server（cron 通道目标态载体），失败不阻断 WS
        await self._maybe_start_pipe_server()
        self._running = True
        logger.info(
            f"WebChannel 已启动: ws://{self.config.host}:{self.config.port}{self.config.path}"
        )
        await self._server.wait_closed()

    async def stop(self) -> None:
        """停止 WebSocket 服务并清理连接."""
        self._running = False
        # 桌面形态（仅管道）：唤醒 start() 的常驻等待
        if self._pipe_only_stop is not None:
            self._pipe_only_stop.set()
            self._pipe_only_stop = None

        all_clients = list(self.clients)
        close_tasks = [client.close(code=1001, reason="server shutdown") for client in all_clients]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._clients_by_key.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # 兜底清理未走正常断连路径的 writer 协程（正常断连已由 unregister_ws 清理）
        await self._shutdown_all_writers()
        if self._pipe_server is not None:
            try:
                await self._pipe_server.stop()
            except Exception as e:  # noqa: BLE001 - 关闭路径容错
                logger.warning("[WebChannel] 管道 server 停止异常: %s", e)
            self._pipe_server = None
        logger.info("WebChannel 已停止")

    async def connect(self) -> None:
        """兼容方法：调用 start."""
        await self.start()

    async def disconnect(self) -> None:
        """兼容方法：调用 stop."""
        await self.stop()

    async def _process_request(self, *args: Any) -> Any:
        """在握手阶段执行 Origin 校验，兼容 legacy/new websockets APIs。"""
        path, request_headers = extract_handshake_request(args)
        origin = get_header_value(request_headers, "Origin")
        enable_origin_check = is_origin_check_enabled()
        if not enable_origin_check:
            logger.info(
                "WebChannel 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
                path,
                origin,
                enable_origin_check,
                True,
            )
            return None

        allowed = is_allowed_browser_origin(origin)
        logger.info(
            "WebChannel 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
            path,
            origin,
            enable_origin_check,
            allowed,
        )
        if allowed:
            return None

        logger.warning(
            "WebChannel 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
            path,
            origin,
        )
        return forbidden_origin_response(args)

    @staticmethod
    def _should_preserve_full_payload(event_name: str) -> bool:
        return (
            event_name in _WEB_FULL_PAYLOAD_EVENT_TYPES
            or event_name.startswith("team.")
            or event_name.startswith("harness.")
        )

    @classmethod
    def _build_event_payload(cls, msg: Message, event_name: str) -> dict[str, Any]:
        """Build the Web event payload without dropping structured control fields."""
        if isinstance(msg.payload, dict):
            if cls._should_preserve_full_payload(event_name):
                payload = {**msg.payload}
                if "session_id" not in payload and msg.session_id:
                    payload["session_id"] = msg.session_id
                if event_name.startswith("chat.") and "request_id" not in payload and msg.id:
                    payload["request_id"] = msg.id
                return payload

            content = str(msg.payload.get("content", "") or "")
            if not content and not getattr(msg, "ok", True) and msg.payload.get("error"):
                content = str(msg.payload.get("error", ""))
            payload = {
                "session_id": msg.session_id,
                "content": content,
            }
            for _key in ("role", "member_name", "member_action", "source_channel", "user_id", "display_name"):
                _val = msg.payload.get(_key)
                if _val is not None:
                    payload[_key] = _val
            if event_name == "chat.final":
                cron_extra = msg.payload.get("cron")
                if isinstance(cron_extra, dict):
                    payload["cron"] = cron_extra
                source = msg.payload.get("source")
                if source:
                    payload["source"] = source
                ptype = msg.payload.get("proactive_type")
                if ptype:
                    payload["proactive_type"] = ptype
                if source == "proactive_recommendation":
                    logger.info(
                        "[WebChannel] proactive push frame: source=%s proactive_type=%s "
                        "content_len=%d payload_keys=%s",
                        source, ptype, len(str(payload.get("content", ""))), list(payload.keys()),
                    )
            return payload

        content = str((msg.params or {}).get("content", "") or "")
        return {
            "session_id": msg.session_id,
            "content": content,
        }

    async def send(
        self,
        msg: Message,
        *,
        routing_target: RoutingTarget | None = None,
    ) -> None:
        """向客户端发送消息。

        V2: 当 routing_target 非空时，按其 routing_keys 精确路由（_clients_by_key）。
        否则回退到全量广播（向后兼容）。
        """
        _pl = getattr(msg, "payload", None) or {}
        _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
        _has_fanout = bool((getattr(msg, "metadata", None) or {}).get("fan_out_targets"))
        logger.debug(
            "[WebChannel] send() called: id=%s event_type=%s payload_et=%s has_fanout=%s"
            " has_routing_target=%s client_count=%s",
            getattr(msg, "id", ""), getattr(msg, "event_type", None), _et,
            _has_fanout, routing_target is not None, len(self.clients),
        )
        # ── 心跳 relay：临时 session_id（heartbeat_{ts}_{suffix}）不匹配任何前端连接，
        # 按常规 session_id 路由会被当作"无连接"丢弃。心跳状态是全局的（非会话级），
        # 前端 setHeartbeatStatus 也是全局 store，因此直接广播给所有 web 客户端。
        # 与 wechat 等 IM 渠道在 send() 中对 HEARTBEAT_RELAY 的专属分支对齐。
        if msg.event_type == EventType.HEARTBEAT_RELAY:
            frame = self._serialize_frame(msg, None)  # 返回 dict，由 writer 统一序列化
            clients = self.clients
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[WebChannel] heartbeat.relay broadcast to %d client(s) id=%s",
                len(clients), getattr(msg, "id", ""),
            )
            return

        # ── 定时任务推 web：原设计绑定 job.session_id，但关闭 tab/换设备后旧会话再无连接，
        # 按 session_id 路由会被丢弃。cron 推送（占位 + 结果）带 payload.cron 标记，普通对话
        # chat.final 不带，以此为识别条件广播给所有 web 客户端。前端 _push_to_targets 已对 web
        # 置空 session_id，shouldHandleSessionEvent 放行，消息进当前活跃会话流（含 placeholder 替换）。
        # 定时任务产物文件（CHAT_FILE，payload 带 cron 标记）走同一豁免——文件与总结文本同源同目标。
        if (
            msg.event_type in (EventType.CHAT_FINAL, EventType.CHAT_FILE)
            and isinstance(msg.payload, dict)
            and isinstance(msg.payload.get("cron"), dict)
        ):
            frame = self._serialize_frame(msg, None)  # 返回 dict，由 writer 统一序列化
            clients = self.clients
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[WebChannel] cron push broadcast to %d client(s) id=%s run_id=%s",
                len(clients), getattr(msg, "id", ""),
                (msg.payload.get("cron") or {}).get("run_id", ""),
            )
            return

        # ── 主动推荐系统通知推 web：与 cron 推送同理——后端主动推、无前端 session_id 绑定，
        # 按 session_id 路由会被当"无 session"丢弃（旧路径 580 行 if not msg.session_id 兜底丢弃）。
        # proactive notification（"今日已达上限"等系统提醒）带 payload.source ==
        # "proactive_notification" 标记，据此广播给所有 web 客户端。前端 shouldHandleSessionEvent
        # 对无 session_id 的 payload 放行，作为普通 assistant 消息渲染。
        if (
            msg.event_type == EventType.CHAT_FINAL
            and isinstance(msg.payload, dict)
            and msg.payload.get("source") == "proactive_notification"
        ):
            frame = self._serialize_frame(msg, None)  # 返回 dict，由 writer 统一序列化
            clients = self.clients
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[WebChannel] proactive_notification broadcast to %d client(s) id=%s",
                len(clients), getattr(msg, "id", ""),
            )
            return

        if msg.type == "res":
            if isinstance(msg.payload, dict):
                res_payload = {**msg.payload}
            elif msg.payload is None:
                res_payload = {}
            else:
                res_payload = {"content": str(msg.payload)}

            frame: dict[str, Any] = {
                "type": "res",
                "id": msg.id,
                "ok": bool(msg.ok),
                "payload": res_payload,
            }
            if not msg.ok:
                # Prefer explicit error; fall back to message (e.g. command.goal
                # unary failures put the human-readable text in payload.message).
                error_text = res_payload.get("error") or res_payload.get("message")
                if isinstance(error_text, str) and error_text:
                    frame["error"] = error_text
                code_text = res_payload.get("code")
                if isinstance(code_text, str) and code_text:
                    frame["code"] = code_text

            ws_set: set[Any] = set()
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            request_ws_id = str(metadata.get("ws_id") or "").strip()
            if request_ws_id:
                ws = self._ws_by_id.get(request_ws_id)
                if ws is not None and not getattr(ws, "closed", False):
                    ws_set.add(ws)

            if not ws_set and routing_target is not None:
                delivery = routing_target.delivery
                if delivery is not None:
                    ws_id = getattr(delivery, "ws_id", "")
                    if ws_id:
                        ws = self._ws_by_id.get(ws_id)
                        if ws is not None and not getattr(ws, "closed", False):
                            ws_set.add(ws)
                if not ws_set:
                    for rk in routing_target.routing_keys:
                        ws_list = self._clients_by_key.get(rk) or []
                        for w in ws_list:
                            if not getattr(w, "closed", False):
                                ws_set.add(w)

            if not ws_set and msg.session_id:
                for rk, ws_list in self._clients_by_key.items():
                    if rk.session_id == msg.session_id:
                        for w in ws_list:
                            if not getattr(w, "closed", False):
                                ws_set.add(w)

            if not ws_set:
                logger.debug(
                    "[WebChannel] response route miss: ws_id=%s session_id=%s id=%s",
                    request_ws_id,
                    msg.session_id,
                    getattr(msg, "id", ""),
                )
                return
            await self._broadcast_to(frame, ws_set)
            return

        # ── V2 精确路由 ──
        if routing_target is not None:
            routing_keys = routing_target.routing_keys
            member_names = list(routing_target.member_names)

            # ── 优先：按 delivery.ws_id 物理寻址 ──
            ws_set: set[Any] = set()
            delivery = routing_target.delivery
            if delivery is not None:
                ws_id = getattr(delivery, "ws_id", "")
                if ws_id:
                    ws = self._ws_by_id.get(ws_id)
                    if ws is not None and not getattr(ws, "closed", False):
                        ws_set.add(ws)

            # ── 兜底：按 routing_keys 5 维逻辑查 _clients_by_key ──
            if not ws_set and routing_keys:
                for rk in routing_keys:
                    ws_list = self._clients_by_key.get(rk) or []
                    for w in ws_list:
                        if not getattr(w, "closed", False):
                            ws_set.add(w)
            if ws_set:
                frame_data = self._serialize_frame(msg, routing_target, member_names=member_names)
                for w in ws_set:
                    self._enqueue_send(w, frame_data)
                return
            # V2 精确路由未命中 —— 回退到 session_id 路由
            logger.debug(
                "[WebChannel] V2 routing miss: looked up %d routing_keys + ws_id=%s,"
                " ws_set empty — falling back to session_id=%s",
                len(routing_keys), getattr(delivery, "ws_id", "") if delivery else "",
                getattr(msg, "session_id", ""),
            )

        # ── 旧路径：优先按请求 metadata.ws_id 物理寻址，再按 session_id 精确路由 ──
        # 普通 stream event（chat.delta/final/usage_summary 等）由 ChannelManager
        # 调 channel.send(msg)，不会携带 RoutingTarget；但原始 Web 请求注入的
        # metadata.ws_id 会经 _chunk_to_message 保留下来。先用它收窄到发起请求的
        # 物理连接，避免同一个 session 桶里的陈旧 ws 一起收到迟到事件。
        ws_set: set[Any] = set()
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        request_ws_id = str(metadata.get("ws_id") or "").strip()
        if request_ws_id:
            ws = self._ws_by_id.get(request_ws_id)
            if ws is not None and not getattr(ws, "closed", False):
                ws_set.add(ws)

        if not ws_set and not msg.session_id:
            logger.warning(
                "[WebChannel] msg has no session_id, cannot route -- "
                "dropping msg id=%s to avoid cross-session broadcast",
                getattr(msg, "id", ""),
            )
            return
        if not ws_set:
            for rk, ws_list in self._clients_by_key.items():
                if rk.session_id == msg.session_id:
                    for w in ws_list:
                        if not getattr(w, "closed", False):
                            ws_set.add(w)
        if not ws_set:
            logger.debug(
                "[WebChannel] session_id=%s has no connected ws, dropping msg id=%s ws_id=%s",
                msg.session_id, getattr(msg, "id", ""), request_ws_id,
            )
            return
        all_clients = ws_set

        # 确定事件名称
        event_name = "chat.final"
        if msg.event_type is not None:
            event_name = msg.event_type.value
        elif isinstance(msg.payload, dict):
            payload_event_type = msg.payload.get("event_type")
            if isinstance(payload_event_type, str) and payload_event_type.strip():
                event_name = payload_event_type.strip()

        payload = self._build_event_payload(msg, event_name)

        # ── V2: 诊断日志 ──
        if routing_target is not None:
            logger.info(
                "[WebChannel] frame: id=%s event=%s intent=%s",
                getattr(msg, "id", ""), event_name, routing_target.intent,
            )
        if getattr(msg, "agent_ref", None):
            payload["agent_ref"] = msg.agent_ref if isinstance(msg.agent_ref, dict) else {
                "mode": getattr(msg.agent_ref, "mode", ""),
                "id": getattr(msg.agent_ref, "id", ""),
            }

        frame_data: dict[str, Any] = {
            "type": "event",
            "event": event_name,
            "payload": payload,
        }
        await self._broadcast_to(frame_data, all_clients)

        # 维护 session busy 状态(供 /ws/git 写操作查询)
        if event_name == "chat.processing_status" and isinstance(payload, dict):
            sid = payload.get("session_id") or msg.session_id
            if sid:
                self._session_busy[sid] = bool(payload.get("is_processing", False))

        # interrupt_result 根据 intent 决定 is_processing 状态
        if event_name == "chat.interrupt_result":
            intent = payload.get("intent", "cancel") if isinstance(payload, dict) else "cancel"
            is_processing = intent in ("pause", "supplement", "resume")
            # 同步更新 busy 映射
            if msg.session_id:
                self._session_busy[msg.session_id] = is_processing
            await self._broadcast_to({
                "type": "event",
                "event": "chat.processing_status",
                "payload": {"session_id": msg.session_id, "is_processing": is_processing},
            }, all_clients)

    def is_session_busy(self, session_id: str) -> bool:
        """查询 session 是否正在执行(agent 处理中)。

        基于 ``chat.processing_status`` 事件维护的映射。
        未跟踪的 session 默认返回 False(不忙碌)。

        供 /ws/git 写操作(如 ``project.git.discard_turn_changes``)在执行前
        校验会话非忙碌,避免与正在进行的 agent 文件写入冲突。
        """
        return self._session_busy.get(session_id, False)

    def get_metadata(self) -> ChannelMetadata:
        """获取 Channel 元数据."""
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="websocket",
            extra={"host": self.config.host, "port": self.config.port, "path": self.config.path},
        )

    # ── 内部实现 ──────────────────────────────────────────

    async def _connection_handler(self, ws: Any, path: str | None = None) -> None:
        raw_path = path if path is not None else getattr(ws, "path", "")
        parsed = urlparse(raw_path)
        request_path = parsed.path or raw_path
        query = parse_qs(parsed.query)
        remote = getattr(ws, "remote_address", None)
        _flat_query = {k: (v[0] if v else "") for k, v in query.items()}

        # ── Path 分发(设计文档 §5.3.7) ──
        # /ws/git → GitDiffWebSocketHandler
        # /ws     → 现有主 RPC
        # 其他    → 1008 close
        if request_path == "/ws/git":
            await self._handle_git_ws_connection(ws, _flat_query, remote)
            return

        if request_path != self.config.path:
            await ws.close(code=1008, reason=f"unsupported path: {request_path}")
            return

        try:
            await self._run_client_session(
                ws, query, remote, self._ws_json_frames(ws), path_label=request_path,
            )
        except WebSocketConnectionClosed as e:  # pragma: no cover - 连接生命周期容错
            logger.info(
                "WebChannel 连接关闭: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": request_path},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:  # pragma: no cover - 连接生命周期容错
            logger.warning(
                "WebChannel 连接异常: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": request_path},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )

    async def _ws_json_frames(self, ws: Any) -> AsyncIterator[dict[str, Any]]:
        """WS 文本帧 → dict 帧迭代器（公共内核的 WS 入口皮）。

        JSON 解析失败回 BAD_REQUEST 错误帧后继续接收（与原 ``_handle_raw_message``
        循环内行为一致）。
        """
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await self.send_response(ws, "", ok=False, error="invalid json", code="BAD_REQUEST")
                continue
            yield data

    async def _run_client_session(
        self,
        ws: Any,
        query: dict[str, list[str]],
        remote: Any,
        frames: AsyncIterator[dict[str, Any]],
        *,
        path_label: str = "",
    ) -> None:
        """单连接公共内核（WS 与命名管道两种传输皮共用）：注册 + 连接钩子 + 帧循环 + 断连清理。

        入站：``frames`` 为已解析的 dict 帧迭代器（WS 文本帧 JSON.parse / 管道长度
        前缀帧），逐帧走 :meth:`_handle_message_dict`。
        出站：统一经 per-ws writer（``_enqueue_send``），WS 发文本帧、管道发长度
        前缀帧，由各自的 ws 皮（websockets 连接 / ``_PipeClientAdapter``）承担。
        """
        _flat_query = {k: (v[0] if v else "") for k, v in query.items()}
        connection_user_id, _user_id = self._resolve_ws_identity(
            ws, _flat_query, remote, route_type="ws",
        )
        uid_marker = "" if connection_user_id else " uid_empty=yes"
        logger.info(
            "WebChannel 新连接: remote=%s query=%s user_id=%r%s",
            remote,
            query,
            connection_user_id,
            uid_marker,
        )

        # ── V2: 从 query 提取身份字段，构造默认 RoutingKey ──
        # session_id 和 agent_id 可能在首条消息中更新
        _app_id = _flat_query.get("app_id", "default")
        _mode = _flat_query.get("mode", "agent")
        _agent_id = _flat_query.get("agent_id", "default")
        _initial_sid = _flat_query.get("session_id", self._make_session_id())
        _initial_rk = RoutingKey(
            user_id=_user_id,
            channel_id=self.channel_id,
            app_id=_app_id,
            agent_ref=AgentRef(mode=_mode, id=_agent_id),
            session_id=_initial_sid,
        )
        await self.register_ws(ws, _initial_rk)
        # 将握手阶段占位 session_id 挂到 ws 上，供 _on_connect 等连接级钩子复用，
        # 确保 connection.ack 与 ws 在 _clients_by_key 中的注册 key 一致，
        # 否则 send() 按 session_id 反查会落空导致 ACK 丢弃。
        # 注：此 sid 仅为传输层占位，首条 chat.send 携带真实 session_id 时会 re-register 覆盖。
        setattr(ws, "_jiuwen_initial_sid", _initial_sid)

        # 触发连接钩子（如发送 connection.ack）
        for hook in self._connect_hooks:
            try:
                result = hook(ws)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # pragma: no cover
                logger.warning(
                    "WebChannel on_connect hook error: %s",
                    format_ws_diagnostics(
                        {"remote": remote, "path": path_label},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )

        try:
            async for data in frames:
                await self._handle_message_dict(ws, data, query)
        finally:
            await self.unregister_ws(ws)

            logger.info(
                "WebChannel 连接清理完成: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": path_label, "clients": len(self._clients_by_key)},
                    describe_ws_peer(ws),
                ),
            )
            # 取出该 ws 关联的 session_ids，清理映射
            ws_id = id(ws)
            disconnected_sessions = self._ws_sessions.pop(ws_id, set())
            logger.info(
                "WebChannel 连接关闭: remote=%s sessions=%s",
                remote,
                disconnected_sessions or "none",
            )
            # 注意:此处不清理 _session_busy。ws 断开不等价于 agent 已停止——
            # 用户关 tab / 刷新 / 网络断开期间,后端 run 仍可能在写文件。若按
            # ws ownership 清掉 busy,新的 discard_turn_changes 会通过 busy 校验,
            # 与仍在运行的 agent 文件写入并发,造成数据损坏。stale busy 的治理
            # 应基于 TTL / 心跳 / agentserver run 状态源,而非 ws 连接状态。
            # 触发断连钩子,传入 session_ids(签名: (ws, session_ids))
            for hook in self._disconnect_hooks:
                try:
                    result = hook(ws, disconnected_sessions)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:  # pragma: no cover
                    logger.warning("WebChannel on_disconnect hook error: %s", e)

    async def _handle_git_ws_connection(
        self,
        ws: Any,
        flat_query: dict[str, str],
        remote: Any,
    ) -> None:
        """处理 /ws/git 路由的连接(设计文档 §5.3.7)。

        构建 ``AgentRef(mode="git", id="diff")`` 哨兵 RoutingKey,
        注册后委托 ``GitDiffWebSocketHandler.handle_connection`` 处理消息循环。
        断连 ``finally`` 先后调 ``unregister_ws(ws)`` 和
        ``git_watcher_registry.cleanup_ws(ws)``,避免 watcher 仍继续轮询推送。
        """
        registry = getattr(self, "git_watcher_registry", None)
        if registry is None:
            await ws.close(code=1011, reason="git watcher registry not available")
            return

        from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import (
            GitDiffWebSocketHandler,
        )
        handler = GitDiffWebSocketHandler(self, registry)

        connection_user_id, _user_id = self._resolve_ws_identity(
            ws, flat_query, remote, route_type="git",
        )
        _app_id = flat_query.get("app_id", "default")
        # session_id 为传输层占位,不是聊天会话(设计文档 §5.3.7)
        _session_id = flat_query.get("session_id") or f"gitws_{uuid.uuid4().hex[:12]}"
        _rk = RoutingKey(
            user_id=_user_id,
            channel_id=self.channel_id,
            app_id=_app_id,
            agent_ref=AgentRef(mode="git", id="diff"),
            session_id=_session_id,
        )
        await self.register_ws(ws, _rk)

        logger.info(
            "[WebChannel] /ws/git 新连接: remote=%s user_id=%r session_id=%s",
            remote,
            connection_user_id,
            _session_id,
        )

        try:
            await handler.handle_connection(ws, flat_query)
        except WebSocketConnectionClosed as e:
            logger.info(
                "[WebChannel] /ws/git 连接关闭: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": "/ws/git"},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:
            logger.warning(
                "[WebChannel] /ws/git 连接异常: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": "/ws/git"},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        finally:
            await self.unregister_ws(ws)
            try:
                registry.cleanup_ws(ws)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[WebChannel] /ws/git cleanup_ws failed: %s", exc,
                )
            logger.info(
                "[WebChannel] /ws/git 连接清理完成: remote=%s",
                remote,
            )

    # ── 命名管道形态（桌面集成：cron 通道目标态载体）────────────────

    async def _maybe_start_pipe_server(self) -> None:
        """桌面形态下并行启动命名管道 server（docs/named-pipe-migration-design.md §5.4）。

        判定：密钥包已由 stdin 下发（``secrets_loaded()``）且携带 ``pipes.cron``
        管道路径。非桌面形态（无密钥包）不起管道 server，行为零变化。
        管道启动失败仅记录日志，不阻断既有 WS 通道。
        """
        if self._pipe_server is not None:
            return
        try:
            from jiuwenswarm.common.secrets_bootstrap import get_secret, secrets_loaded
        except Exception:  # pragma: no cover - 防御：模块不可用时视为非桌面形态
            return
        if not secrets_loaded():
            return
        pipe_path = str(get_secret("pipes.cron", "") or "").strip()
        if not pipe_path:
            return
        if sys.platform != "win32":
            logger.warning(
                "[WebChannel] 命名管道仅支持 Windows，跳过 cron 管道 server: %s", pipe_path,
            )
            return
        try:
            from jiuwenswarm.common.np_transport import make_image_verifier, serve_pipe
        except Exception as exc:  # pragma: no cover - pywin32 缺失时降级 WS 单通道
            logger.error("[WebChannel] 命名管道传输不可用，跳过 cron 管道 server: %s", exc)
            return

        # 对端进程身份校验：cron 由桌面主进程调用，Gateway 控制由同一后端
        # 运行时的 AgentServer 调用。uv venv 在 Windows 上可能是 trampoline，
        # 此时真实进程镜像为 sys._base_executable；exe 发布形态两者恒等。
        desktop_exe = str(get_secret("desktopExe", "") or "").strip()
        allowed_images = {
            path
            for path in (desktop_exe, sys.executable, sys._base_executable)
            if path
        }
        verify_client = make_image_verifier(allowed_images) if allowed_images else None
        try:
            self._pipe_server = await serve_pipe(
                pipe_path,
                self._handle_pipe_connection,
                verify_client=verify_client,
            )
        except Exception as exc:  # noqa: BLE001 - 管道启动失败不阻断 WS 通道
            logger.error("[WebChannel] cron 管道 server 启动失败（仅 WS 可用）: %s", exc)
            return
        logger.info(
            "[WebChannel] cron 命名管道 server 已启动: %s（verify_client=%s）",
            pipe_path,
            "on" if verify_client is not None else "off",
        )

    async def _handle_pipe_connection(self, stream: Any) -> None:
        """管道连接处理：首帧 auth（e2aToken）→ 通过后进入与 WS 相同的会话内核。

        鉴权失败/首帧异常即返回（``PipeServer`` 兜底关管）。连接级异常只影响
        本连接，不波及其他管道连接与 WS 通道。
        """
        try:
            from jiuwenswarm.common.np_transport import FrameCodecError, PipeError
            from jiuwenswarm.common.secrets_bootstrap import get_secret

            try:
                first = await stream.recv_frame(timeout=_PIPE_AUTH_TIMEOUT_SECONDS)
            except (FrameCodecError, PipeError) as exc:
                logger.info("[WebChannel] 管道连接首帧读取失败，断开: %s", exc)
                return
            token = (
                first.get("token")
                if isinstance(first, dict) and first.get("type") == "auth"
                else None
            )
            expected = str(get_secret("e2aToken", "") or "")
            if (
                not expected
                or not isinstance(token, str)
                or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
            ):
                logger.warning("[WebChannel] 管道连接 auth 校验失败，断开")
                return
            ws = _PipeClientAdapter(stream)
            await self._run_client_session(
                ws, {}, _PIPE_REMOTE_LABEL, self._pipe_frames(stream), path_label="np:cron",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接级容错
            logger.warning("[WebChannel] 管道连接异常: %s", exc)

    async def _pipe_frames(self, stream: Any) -> AsyncIterator[dict[str, Any]]:
        """管道长度前缀帧 → dict 帧迭代器；断管即结束，协议错误记录后关管。"""
        from jiuwenswarm.common.np_transport import FrameCodecError, PipeClosedError, PipeError

        while not stream.closed:
            try:
                frame = await stream.recv_frame()
            except PipeClosedError:
                return
            except (FrameCodecError, PipeError) as exc:
                logger.warning("[WebChannel] 管道帧协议/传输错误，关闭连接: %s", exc)
                return
            yield frame

    async def _handle_raw_message(self, ws: Any, raw: str, query: dict[str, list[str]]) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_response(ws, "", ok=False, error="invalid json", code="BAD_REQUEST")
            return
        await self._handle_message_dict(ws, data, query)

    async def _handle_message_dict(
        self, ws: Any, data: Any, query: dict[str, list[str]],
    ) -> None:
        """消息处理公共内核（收 dict / 发 dict）：WS 与命名管道传输皮共用。"""
        if not isinstance(data, dict):
            await self.send_response(ws, "", ok=False, error="invalid request", code="BAD_REQUEST")
            return

        req_type = data.get("type")
        req_id = data.get("id")
        method = data.get("method")
        params = data.get("params")

        if req_type != "req" or not isinstance(req_id, str) or not isinstance(method, str):
            await self.send_response(
                ws,
                req_id if isinstance(req_id, str) else "",
                ok=False,
                error="invalid request",
                code="BAD_REQUEST",
            )
            return
        if not isinstance(params, dict):
            params = {}

        # ── V2: session_id 解析 ──
        # 请求自带 session_id（如 chat.send）→ 用它更新 ws 路由注册。
        # 请求未带 session_id（如 memory.compute 心跳、updater.check、config.get
        # 等 ws 层 keepalive / 拉取请求）→ 这类请求与 session 无关，
        # 仅合成一个临时 id 供后续 Message 构造使用，但【不】参与 register_ws，
        # 保留 ws 上一次的真实 RoutingKey，避免把 ws 从其所属 team session 摘除。
        _explicit_session_id = params.get("session_id")
        has_explicit_session = (
            isinstance(_explicit_session_id, str) and bool(_explicit_session_id)
        )
        session_id = _explicit_session_id if has_explicit_session else self._make_session_id()

        # 追踪 ws → 真实 session_id，用于断连清理/日志。
        # 与 register_ws 一致：仅显式 session 入集；临时 id 只供 Message 构造，避免膨胀。
        if has_explicit_session:
            ws_id = id(ws)
            sessions = self._ws_sessions.get(ws_id)
            if sessions is None:
                sessions = set()
                self._ws_sessions[ws_id] = sessions
            sessions.add(session_id)

        params = await self._process_files(params)

        # ── V2: 用实际的 session_id / mode / agent_id 更新 ws 注册 ──
        _flat_query = {k: (v[0] if v else "") for k, v in query.items()}
        _mode = params.get("mode", "agent")
        _agent_id = params.get("agent_id", "default")
        _app_id = _flat_query.get("app_id", "default")
        req_user_id = self._connection_user_id(ws)
        if has_explicit_session:
            _rk = RoutingKey(
                user_id=self._routing_key_user_id(req_user_id, getattr(ws, "remote_address", None)),
                channel_id=self.channel_id,
                app_id=_app_id,
                agent_ref=AgentRef(mode=_mode, id=_agent_id),
                session_id=session_id,
            )
            await self.register_ws(ws, _rk)
        # else: ws 层心跳 / 拉取请求，不更新路由注册，沿用 ws 已有的 RoutingKey。

        # Preserve client top-level is_stream (e.g. command.goal set/resume).
        # chat.send / history.get still become stream in _normalize_gateway_message
        # even when the client omits this field.
        user_message = Message(
            id=req_id,
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=self._parse_req_method(method),
            mode=self._parse_mode(params.get("mode")),
            is_stream=bool(data.get("is_stream", False)),
            app_id=_app_id,
            agent_ref={"mode": _mode, "id": _agent_id},
            user_id=req_user_id,
            metadata={
                "query": query,
                "method": method,
                # V2: 注入 ws_id 供 MessageHandler 构造 WebDeliveryTarget(ws_id=真值)。
                "ws_id": getattr(ws, "_jiuwen_ws_id", ""),
                "user_id": req_user_id,
            },
        )

        # 发布到 route 或回调
        handler = self._method_handlers.get(method)
        handler_already_called = False
        if method in _HANDLER_BEFORE_CALLBACK_METHODS and handler is not None:
            handler_already_called = await self._invoke_method_handler(
                _MethodHandlerInvocation(
                    ws, method, req_id, params, session_id, handler,
                ),
            )
            if not handler_already_called:
                return

        handled_by_callback = False
        if self._on_message_cb is not None:
            result = self._on_message_cb(user_message)
            if inspect.isawaitable(result):
                result = await result
            handled_by_callback = bool(result)
        else:
            await self.bus.publish_user_messages(user_message)

        if handled_by_callback:
            return
        if handler_already_called:
            return

        # 路由到已注册的方法处理器
        if handler is not None:
            await self._invoke_method_handler(
                _MethodHandlerInvocation(
                    ws, method, req_id, params, session_id, handler,
                ),
            )
        else:
            await self.send_response(
                ws, req_id, ok=False,
                error=f"unknown method: {method}", code="METHOD_NOT_FOUND",
            )

    async def _broadcast_to(self, frame: dict[str, Any], clients: set[Any]) -> None:
        """向指定 clients 集合广播帧（走 per-ws writer，非阻塞入队）.

        入队 dict，由 writer 统一序列化一次，避免此处预 dumps。
        """
        if not clients:
            return
        for client in clients:
            self._enqueue_send(client, frame)

    # ── BaseWsChannel 抽象方法 ──

    def _serialize_frame(
        self,
        msg: Any,
        routing_target: RoutingTarget | None = None,
        *,
        member_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """将 Message 转为 Web 前端帧 dict（由 writer 统一序列化）."""
        event_name = "chat.final"
        if getattr(msg, "event_type", None) is not None:
            event_name = msg.event_type.value
        elif isinstance(getattr(msg, "payload", None), dict):
            et = msg.payload.get("event_type")
            if isinstance(et, str) and et.strip():
                event_name = et.strip()

        payload: dict[str, Any] = {}
        if isinstance(msg.payload, dict):
            payload = {**msg.payload}
            if "session_id" not in payload and getattr(msg, "session_id", None):
                payload["session_id"] = msg.session_id
        elif getattr(msg, "payload", None) is not None:
            payload = {"session_id": getattr(msg, "session_id", None), "content": str(msg.payload)}
        else:
            payload = {"session_id": getattr(msg, "session_id", None), "content": ""}

        agent_ref = getattr(msg, "agent_ref", None)
        if agent_ref:
            payload["agent_ref"] = agent_ref if isinstance(agent_ref, dict) else {
                "mode": getattr(agent_ref, "mode", ""),
                "id": getattr(agent_ref, "id", ""),
            }

        frame: dict[str, Any] = {
            "type": "event",
            "event": event_name,
            "payload": payload,
        }
        return frame

    @staticmethod
    def _parse_req_method(method: str) -> ReqMethod | None:
        for item in ReqMethod:
            if item.value == method:
                return item
        return None

    @staticmethod
    def _parse_mode(raw_mode: Any) -> Mode:
        return Mode.from_raw(raw_mode, default=Mode.AGENT)

    @staticmethod
    def _make_session_id() -> str:
        # 与前端 generateSessionId 保持一致：毫秒时间戳(16进制) + 6位随机16进制
        ts = format(int(time.time() * 1000), "x")
        suffix = secrets.token_hex(3)
        return f"sess_{ts}_{suffix}"
