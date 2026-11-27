# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""WebChannel - WebSocket 通道实现.

提供可扩展的方法处理器注册机制 (`register_method`) 和连接钩子 (`on_connect`)，
使上层应用可以灵活控制每个 req method 的行为，而无需修改通道本身。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import aiohttp

from jiuwenclaw.agentserver.session_metadata import get_resolved_project_dir
from jiuwenclaw.utils import get_agent_sessions_dir
from jiuwenclaw.channel.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenclaw.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_allowed_browser_origin,
)
from jiuwenclaw.schema.message import Message, Mode, ReqMethod

logger = logging.getLogger(__name__)

_CLIENT_SEND_QUEUE_MAX = 256
_CLIENT_WRITE_LIMIT_BYTES = 1024 * 1024
_MAX_WS_MESSAGE_BYTES = 4 * 1024 * 1024

# ── 类型别名 ──────────────────────────────────────────────
# 方法处理器签名: (ws, req_id, params, session_id) -> None
MethodHandler = Callable[..., Awaitable[None]]
# 连接钩子签名: (ws) -> None | Awaitable[None]
ConnectHook = Callable[..., Any]


@dataclass
class WebChannelConfig:
    """WebChannel 配置."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 19000
    path: str = "/ws"
    allow_from: list[str] = field(default_factory=list)


class WebChannel(BaseChannel):
    """Web 前端 WebSocket 通道.

    核心职责：
    1. 管理 WebSocket 连接生命周期
    2. 解析帧协议 (req / res / event)
    3. 将入站消息发布到 RobotMessageRouter
    4. 将方法路由委托给通过 `register_method` 注册的处理器
    """

    name = "web"

    def __init__(self, config: WebChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: WebChannelConfig = config
        self._server: Any = None
        self._clients: set[Any] = set()
        self._client_send_queues: dict[Any, asyncio.Queue[str]] = {}
        self._client_send_tasks: dict[Any, asyncio.Task[None]] = {}
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._method_handlers: dict[str, MethodHandler] = {}
        self._connect_hooks: list[ConnectHook] = []

    # ── 公共属性 ──────────────────────────────────────────

    @property
    def channel_id(self) -> str:
        """返回唯一 Channel 标识."""
        return self.name

    @property
    def clients(self) -> set[Any]:
        """当前活跃的 WebSocket 客户端集合（只读副本）."""
        return set(self._clients)

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

    def on_message(self, callback: Callable[[Message], None]) -> None:
        """注册消息接收回调（替代默认的 router.publish_user_messages）。"""
        self._on_message_cb = callback

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
            await self._send_to_client(ws, frame)
        except Exception as e:
            if bool(getattr(ws, "closed", False)):
                logger.debug("WebChannel send_response skipped on closed websocket: id={} err={}", req_id, e)
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
            await self._send_to_client(ws, frame)
        except Exception as e:
            if bool(getattr(ws, "closed", False)):
                logger.debug("WebChannel send_event skipped on closed websocket: event={} err={}", event, e)
                return
            raise

    async def broadcast_event(
            self,
            event: str,
            payload: dict[str, Any],
            *,
            seq: int | None = None,
            stream_id: str | None = None,
    ) -> None:
        """向所有已连接客户端广播 ``event`` 帧."""
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        if seq is not None:
            frame["seq"] = seq
        if stream_id is not None:
            frame["stream_id"] = stream_id
        await self._broadcast(frame)

    async def _download_file(self, url: str) -> bytes | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        logger.warning("WebChannel 文件下载失败: {}, 状态码: {}", url, response.status)
                        return None
        except Exception as e:
            logger.warning("WebChannel 文件下载异常: {}, 错误: {}", url, e)
            return None

    async def _process_files(self, params: dict[str, Any], session_id: str) -> dict[str, Any]:
        files = params.get("files")
        if not files or not isinstance(files, list):
            return params

        downloaded_files = []
        workspace_dir = get_resolved_project_dir(session_id, str(get_agent_sessions_dir()))

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
                        logger.warning("WebChannel 文件保存失败: {}", e)

            downloaded_files.append(file_info)

        params["files"] = downloaded_files
        return params

    # ── Channel 生命周期 ──────────────────────────────────

    async def start(self) -> None:
        """启动 WebSocket 服务并监听客户端连接."""
        if self._running:
            logger.warning("WebChannel 已在运行")
            return
        if not self.config.enabled:
            logger.warning("WebChannel 未启用（enabled=False）")
            return

        try:
            from websockets.legacy.server import serve as ws_serve
        except Exception:  # pragma: no cover
            import websockets

            ws_serve = websockets.serve

        self._server = await ws_serve(
            self._connection_handler,
            self.config.host,
            self.config.port,
            process_request=self._process_request,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
            max_size=_MAX_WS_MESSAGE_BYTES,
            write_limit=_CLIENT_WRITE_LIMIT_BYTES,
        )
        self._running = True
        logger.info(
            f"WebChannel 已启动: ws://{self.config.host}:{self.config.port}{self.config.path}"
        )
        await self._server.wait_closed()

    async def stop(self) -> None:
        """停止 WebSocket 服务并清理连接."""
        self._running = False

        close_tasks = [client.close(code=1001, reason="server shutdown") for client in list(self._clients)]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._clients.clear()
        await self._cleanup_client_send_tasks()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
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
        allowed = is_allowed_browser_origin(origin)
        logger.info(
            "WebChannel 握手检查 path=%s origin=%s allowed=%s",
            path,
            origin,
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

    async def send(self, msg: Message) -> None:
        """向客户端发送消息（默认封装为 event 帧广播）."""
        if not self._clients:
            return

        # 响应帧：优先按 res 语义透传，避免误封装为 chat.final
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
                error_text = res_payload.get("error")
                if isinstance(error_text, str) and error_text:
                    frame["error"] = error_text
                code_text = res_payload.get("code")
                if isinstance(code_text, str) and code_text:
                    frame["code"] = code_text
            await self._broadcast(frame)
            return

        # 确定事件名称
        event_name = "chat.final"
        if msg.event_type is not None:
            event_name = msg.event_type.value
        elif isinstance(msg.payload, dict):
            payload_event_type = msg.payload.get("event_type")
            if isinstance(payload_event_type, str) and payload_event_type.strip():
                event_name = payload_event_type.strip()

        # 根据事件类型构造 payload
        payload = {}

        if isinstance(msg.payload, dict):
            # 对于需要传递完整结构化数据的事件类型
            if event_name in ("connection.ack", "todo.updated", "chat.tool_call", "chat.tool_result",
                    "chat.processing_status", "chat.interrupt_result", "chat.evolution_status", "chat.tool_calls.delta",
                    "chat.error", "heartbeat.relay",
                    "context.compressed", "context.usage", "chat.ask_user_question", "chat.invocation_paused",
                    "chat.subtask_update",
                    "history.message",
                    "chat.session_result", "chat.usage_metadata",
                    "chat.usage_summary") or event_name.startswith("skilldev.") or event_name.startswith("team."):
                # 传递完整 payload，保留所有字段
                payload = {**msg.payload}
                # 确保包含 session_id
                if "session_id" not in payload and msg.session_id:
                    payload["session_id"] = msg.session_id
            else:
                # 对于纯文本消息（chat.delta, chat.final, chat.error 等），提取 content
                content = str(msg.payload.get("content", "") or "")
                if not content and not getattr(msg, "ok", True) and msg.payload.get("error"):
                    content = str(msg.payload.get("error", ""))
                payload = {
                    "session_id": msg.session_id,
                    "content": content,
                }
                # 定时任务推送：附带 cron 元数据，供前端识别并替换占位消息（避免误写入流式气泡）
                if event_name == "chat.final":
                    cron_extra = msg.payload.get("cron")
                    if isinstance(cron_extra, dict):
                        payload["cron"] = cron_extra
        else:
            # payload 不是 dict，尝试从 params 提取
            content = str((msg.params or {}).get("content", "") or "")
            payload = {
                "session_id": msg.session_id,
                "content": content,
            }

        frame = {
            "type": "event",
            "event": event_name,
            "payload": payload,
        }
        await self._broadcast(frame)

        # interrupt_result 根据 intent 决定 is_processing 状态
        if event_name == "chat.interrupt_result":
            intent = payload.get("intent", "cancel") if isinstance(payload, dict) else "cancel"
            is_processing = intent in ("pause", "supplement", "resume")
            await self._broadcast({
                "type": "event",
                "event": "chat.processing_status",
                "payload": {"session_id": msg.session_id, "is_processing": is_processing},
            })

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
        if request_path != self.config.path:
            await ws.close(code=1008, reason=f"unsupported path: {request_path}")
            return

        query = parse_qs(parsed.query)
        remote = getattr(ws, "remote_address", None)
        self._clients.add(ws)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_CLIENT_SEND_QUEUE_MAX)
        self._client_send_queues[ws] = queue
        self._client_send_tasks[ws] = asyncio.create_task(
            self._client_send_loop(ws, queue)
        )
        logger.info(f"WebChannel 新连接: remote={remote} query={query}")

        # 触发连接钩子（如发送 connection.ack）
        for hook in self._connect_hooks:
            try:
                result = hook(ws)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # pragma: no cover
                logger.warning("WebChannel on_connect hook error: {}", e)

        try:
            async for raw in ws:
                await self._handle_raw_message(ws, raw, query)
        except Exception as e:  # pragma: no cover - 连接生命周期容错
            logger.warning("WebChannel 连接异常: %s", e)
        finally:
            self._clients.discard(ws)
            queue = self._client_send_queues.pop(ws, None)
            task = self._client_send_tasks.pop(ws, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if queue is not None:
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        break
            logger.info(f"WebChannel 连接关闭: remote={remote}")

    async def _handle_raw_message(self, ws: Any, raw: str, query: dict[str, list[str]]) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_response(ws, "", ok=False, error="invalid json", code="BAD_REQUEST")
            return

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

        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = self._make_session_id()

        params = await self._process_files(params, session_id)

        # skilldev.chat / skilldev.start / skilldev.respond 需走流式路径才能实时推送事件
        stream_methods = ("skilldev.chat", "skilldev.start", "skilldev.respond")
        is_stream = method in stream_methods

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
            is_stream=is_stream,
            metadata={"query": query, "method": method},
        )

        # 发布到 route 或回调
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

        # 路由到已注册的方法处理器
        handler = self._method_handlers.get(method)
        if handler is not None:
            try:
                await handler(ws, req_id, params, session_id)
            except Exception as e:
                # 客户端断开（如服务关闭时 code=1001）不再尝试回包，避免二次异常噪音。
                ws_closed = bool(getattr(ws, "closed", False))
                if ws_closed:
                    logger.warning(
                        "WebChannel method handler aborted on closed websocket ({}): {}",
                        method, e,
                    )
                    return

                logger.error("WebChannel method handler error ({}): {}", method, e)
                try:
                    await self.send_response(
                        ws, req_id, ok=False,
                        error=f"handler error: {e}", code="INTERNAL_ERROR",
                    )
                except Exception as send_err:
                    logger.warning(
                        "WebChannel failed to send handler error response ({}): {}",
                        method, send_err,
                    )
        else:
            await self.send_response(
                ws, req_id, ok=False,
                error=f"unknown method: {method}", code="METHOD_NOT_FOUND",
            )

    async def _send_to_client(self, ws: Any, frame: dict[str, Any]) -> None:
        data = json.dumps(frame, ensure_ascii=False)
        queue = self._client_send_queues.get(ws)
        task = self._client_send_tasks.get(ws)
        if queue is None or task is None or task.done():
            await ws.send(data)
            return
        await queue.put(data)

    async def _cleanup_client_send_tasks(self) -> None:
        tasks = list(self._client_send_tasks.values())
        self._client_send_tasks.clear()
        self._client_send_queues.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _client_send_loop(self, ws: Any, queue: asyncio.Queue[str]) -> None:
        try:
            while True:
                data = await queue.get()
                try:
                    await ws.send(data)
                except Exception as e:
                    if not bool(getattr(ws, "closed", False)):
                        logger.warning("WebChannel client send loop failed: %s", e)
                    break
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        data = json.dumps(frame, ensure_ascii=False)
        if not self._clients:
            return

        _bc_t0 = asyncio.get_event_loop().time()
        dropped = 0
        for client, queue in list(self._client_send_queues.items()):
            if client not in self._clients:
                continue
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                dropped += 1
        _bc_elapsed = (asyncio.get_event_loop().time() - _bc_t0) * 1000
        event_name = str(frame.get("event") or "") if frame.get("type") == "event" else ""
        is_stream_chunk = event_name in {
            "chat.delta",
            "chat.reasoning",
            "chat.tool_calls.delta",
        }
        log_fn = logger.debug if is_stream_chunk else logger.info
        log_fn(
            "[STREAM_DIAG][WebChannel] broadcast clients=%d elapsed=%.1fms len=%d dropped=%d event=%s",
            len(self._clients), _bc_elapsed, len(data), dropped, event_name or "n/a",
        )
        if dropped:
            logger.warning(
                "WebChannel client send queue full, dropped messages for %d clients",
                dropped,
            )

    @staticmethod
    def _parse_req_method(method: str) -> ReqMethod | None:
        for item in ReqMethod:
            if item.value == method:
                return item
        return None

    @staticmethod
    def _parse_mode(raw_mode: Any) -> Mode:
        return Mode.from_raw(raw_mode, default=Mode.AGENT_PLAN)

    @staticmethod
    def _make_session_id() -> str:
        # 与前端 generateSessionId 保持一致：毫秒时间戳(16进制) + 6位随机16进制
        ts = format(int(time.time() * 1000), "x")
        suffix = secrets.token_hex(3)
        return f"sess_{ts}_{suffix}"
