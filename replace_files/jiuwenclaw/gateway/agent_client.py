# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServerClient - Gateway 与 AgentServer 的 WebSocket 客户端."""

from __future__ import annotations

import logging
import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - compatibility fallback
    ConnectionClosed = Exception  # type: ignore[assignment]

from jiuwenclaw.e2a.constants import (
    E2A_WIRE_SERVER_PUSH_KEY,
    FILE_TRANSFER_START,
    FILE_TRANSFER_CHUNK,
    FILE_TRANSFER_COMPLETE,
)
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenclaw.utils import FileTransferStartParams


logger = logging.getLogger(__name__)
_STREAM_TRAILING_MESSAGE_GRACE_SECONDS = 0.7
_UNARY_REQUEST_TIMEOUT_SECONDS = 60.0
_WS_MAX_SIZE = 8 * 2**20
_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0


class AgentServerConnectionError(RuntimeError):
    """AgentServer WebSocket 断开或尚未完成就绪握手。"""


def _wire_request_id_key(request_id: Any) -> str:
    """与 AgentServer 回包 ``request_id`` 对齐：统一为 str，避免 JSON 数字/字符串导致队列键不一致。"""
    if request_id is None:
        return ""
    return str(request_id)


def _build_ws_origin(uri: str) -> str | None:
    """将 ws/wss URI 转为标准浏览器 Origin。"""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None

    if not parsed.netloc:
        return None

    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.netloc}"


class AgentServerClient(ABC):
    """AgentServer WebSocket 客户端接口."""

    @abstractmethod
    async def connect(self, uri: str) -> None:
        """建立与 AgentServer 的 WebSocket 连接."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接."""
        ...

    @abstractmethod
    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        """缓存或更新服务端配置快照，供自定义 client 后续使用."""
        ...

    @abstractmethod
    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        """发送 E2A 信封，等待完整响应."""
        ...

    @abstractmethod
    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        """发送 E2A 信封，流式接收响应."""
        ...


def _e2a_to_wire(envelope: E2AEnvelope) -> dict[str, Any]:
    """E2AEnvelope → WebSocket JSON（与 AgentServer from_dict 对齐）。"""
    return envelope.to_dict()


class WebSocketAgentServerClient(AgentServerClient):
    """
    基于 websockets 的 AgentServer WebSocket 客户端实现。

    协议约定：
    - 发送：JSON 对象为 E2AEnvelope.to_dict()（含 protocol_version、method、channel、params、is_stream 等）。
    - 接收（非流式）：一条 **E2AResponse** 线 JSON（或过渡期 legacy AgentResponse 形），解析为 AgentResponse。
    - 接收（流式）：多条 E2AResponse 线 JSON（或 legacy chunk），解析为 AgentResponseChunk。
    """

    def __init__(self, *, ping_interval: float | None = 30.0, ping_timeout: float | None = 300.0) -> None:
        self._uri: str | None = None
        self._target_uri: str | None = None
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._server_ready: bool = False
        # 消息分发机制：根据 request_id 路由到对应队列
        self._message_queues: dict[str, asyncio.Queue] = {}
        self._queue_lock = asyncio.Lock()  # 保护队列操作的锁
        self._cancelled_request_ids: set[str] = set()  # 已取消但等待清理的 request_id
        self._receiver_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._running = False
        self._shutdown_requested = False
        # AgentServer send_push：旁路投递，勿进入与 request_id 绑定的 RPC 等待队列
        self._on_server_push: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        """注册 Agent 主动推送处理回调（metadata 含 ``E2A_WIRE_SERVER_PUSH_KEY`` 的帧）。"""
        self._on_server_push = handler

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        """默认 WebSocket client 不处理服务端配置缓存，留给扩展 client 自行实现."""
        return None

    @property
    def server_ready(self) -> bool:
        """AgentServer 是否已发送 connection.ack 确认就绪."""
        return self._server_ready

    async def connect(self, uri: str) -> None:
        if self._ws is not None or self._reconnect_task is not None:
            await self.disconnect()
        self._target_uri = uri
        self._shutdown_requested = False
        await self._connect_once(uri)

    async def _open_websocket(self, uri: str) -> Any:
        """建立底层 WebSocket；拆分为方法便于连接管理和测试。"""
        origin = _build_ws_origin(uri)
        try:
            from websockets.legacy.client import connect as legacy_connect
            connect_fn = legacy_connect
        except ImportError:
            import websockets
            connect_fn = websockets.connect
        return await connect_fn(
            uri,
            origin=origin,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            close_timeout=5.0,
            max_size=_WS_MAX_SIZE,
        )

    async def _connect_once(self, uri: str) -> None:
        """建立一次连接；只有收到 connection.ack 后才发布为可用连接。"""
        async with self._connect_lock:
            if self._shutdown_requested:
                raise AgentServerConnectionError("AgentServer 客户端已停止")
            if self._connection_is_usable():
                return

            stale_ws = self._ws
            self._ws = None
            self._server_ready = False
            if stale_ws is not None:
                try:
                    await stale_ws.close()
                except Exception as exc:
                    logger.debug("关闭旧 AgentServer WebSocket 失败: %s", exc)

            logger.info("[WebSocketAgentServerClient] 正在连接: %s", uri)
            ws = await self._open_websocket(uri)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(raw)
                logger.info(
                    "[WebSocketAgentServerClient] connect 首帧: type=%s event=%s request_id=%s",
                    data.get("type"),
                    data.get("event"),
                    data.get("request_id"),
                )
                if not (
                    data.get("type") == "event"
                    and data.get("event") == "connection.ack"
                ):
                    raise AgentServerConnectionError(
                        "AgentServer 首帧不是 connection.ack，连接不可用"
                    )
            except Exception:
                try:
                    await ws.close()
                except Exception as close_exc:
                    logger.debug("ACK 失败后关闭 AgentServer WebSocket 失败: %s", close_exc)
                raise

            if self._shutdown_requested:
                await ws.close()
                raise AgentServerConnectionError("AgentServer 客户端已停止")

            self._ws = ws
            self._uri = uri
            self._server_ready = True
            self._running = True
            self._receiver_task = asyncio.create_task(self._message_receiver_loop(ws))
            logger.info("[WebSocketAgentServerClient] 已连接并收到 connection.ack: %s", uri)
            logger.info("[WebSocketAgentServerClient] 消息接收任务已启动")

    async def _message_receiver_loop(self, ws: Any | None = None) -> None:
        """后台任务：从 WebSocket 接收消息并根据 request_id 分发到对应队列."""
        active_ws = ws if ws is not None else self._ws
        disconnect_error: AgentServerConnectionError | None = None
        try:
            while self._running and active_ws is not None and self._ws is active_ws:
                try:
                    raw = await active_ws.recv()
                    data = json.loads(raw)
                    meta = data.get("metadata")
                    if isinstance(meta, dict) and meta.get(E2A_WIRE_SERVER_PUSH_KEY):
                        if self._on_server_push is not None:
                            asyncio.create_task(self._on_server_push(data))
                        else:
                            logger.warning(
                                "[WebSocketAgentServerClient] 收到 server_push 但未注册 handler，已丢弃: "
                                "request_id=%s",
                                data.get("request_id"),
                            )
                        continue
                    request_id = _wire_request_id_key(data.get("request_id"))

                    # 使用锁保护队列访问，避免竞态条件
                    async with self._queue_lock:
                        # 检查是否是已取消的请求，静默丢弃消息
                        if request_id in self._cancelled_request_ids:
                            logger.debug(
                                "[WebSocketAgentServerClient] 收到已取消请求的残余消息，已丢弃: request_id=%s",
                                request_id
                            )
                            continue

                        if request_id and request_id in self._message_queues:
                            await self._message_queues[request_id].put(data)
                        else:
                            # 没有对应的队列（非预期情况）
                            logger.debug(
                                "[WebSocketAgentServerClient] 收到无目标队列的消息: request_id=%s",
                                request_id
                            )
                except asyncio.CancelledError:
                    break
                except ConnectionClosed as exc:
                    disconnect_error = AgentServerConnectionError(
                        "AgentServer WebSocket 已断开 "
                        f"(code={getattr(exc, 'code', None)}, reason={getattr(exc, 'reason', None)})"
                    )
                    logger.warning(
                        "[WebSocketAgentServerClient] WebSocket 连接关闭: code=%s reason=%s",
                        getattr(exc, "code", None),
                        getattr(exc, "reason", None),
                    )
                    break
                except Exception as e:
                    disconnect_error = AgentServerConnectionError(
                        f"AgentServer WebSocket 接收失败: {e}"
                    )
                    logger.exception("[WebSocketAgentServerClient] 消息接收循环异常退出: %s", e)
                    break
        finally:
            if disconnect_error is not None:
                await self._handle_connection_lost(active_ws, disconnect_error)
            logger.info("[WebSocketAgentServerClient] 消息接收任务已停止")

    async def _handle_connection_lost(
        self, ws: Any, error: AgentServerConnectionError
    ) -> None:
        """下线失效连接、失败所有等待者，并启动唯一重连任务。"""
        if self._ws is not ws:
            return
        self._running = False
        self._server_ready = False
        self._ws = None
        self._uri = None
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("断链后关闭 AgentServer WebSocket 失败: %s", exc)
        await self._fail_all_pending_requests(error)
        self._schedule_reconnect()

    async def _fail_all_pending_requests(self, error: BaseException) -> None:
        async with self._queue_lock:
            for queue in self._message_queues.values():
                queue.put_nowait(error)

    def _schedule_reconnect(self) -> None:
        if self._shutdown_requested or not self._target_uri:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="agent-server-reconnect"
        )

    async def _reconnect_loop(self) -> None:
        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        try:
            while not self._shutdown_requested and self._target_uri:
                logger.info(
                    "[WebSocketAgentServerClient] %.1f 秒后重连 AgentServer: %s",
                    delay,
                    self._target_uri,
                )
                await asyncio.sleep(delay)
                try:
                    await self._connect_once(self._target_uri)
                    logger.info("[WebSocketAgentServerClient] AgentServer 重连成功")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[WebSocketAgentServerClient] AgentServer 重连失败: %s", exc
                    )
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    async def disconnect(self) -> None:
        self._shutdown_requested = True
        self._target_uri = None
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if reconnect_task is not None and reconnect_task is not asyncio.current_task():
            reconnect_task.cancel()
            try:
                await reconnect_task
            except asyncio.CancelledError:
                pass

        # 停止接收任务
        self._running = False
        self._server_ready = False
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None

        # 唤醒等待中的 RPC；各请求会在 finally 中自行移除队列。
        await self._fail_all_pending_requests(
            AgentServerConnectionError("AgentServer WebSocket 客户端已断开")
        )

        # 关闭 WebSocket
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception as e:
            logger.warning("关闭 AgentServer WebSocket 时异常: %s", e)
        finally:
            self._ws = None
            self._uri = None
        logger.info("[WebSocketAgentServerClient] 已断开")

    def _ensure_connected(self) -> None:
        if not self._connection_is_usable():
            raise AgentServerConnectionError(
                "AgentServer WebSocket 未连接、已关闭或尚未收到 connection.ack"
            )

    def _connection_is_usable(self) -> bool:
        ws = self._ws
        if ws is None or not self._server_ready:
            return False
        return not bool(getattr(ws, "closed", False))

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        self._ensure_connected()
        # 非流式 API 必须与 AgentServer 的 unary 路径一致；忽略信封上误带的 is_stream=True。
        envelope.is_stream = False
        rid = _wire_request_id_key(envelope.request_id)
        logger.info(
            "[E2A][out][nostream] request_id=%s channel=%s method=%s is_stream=%s",
            rid,
            envelope.channel,
            envelope.method,
            envelope.is_stream,
        )
        logger.debug(
            "[WebSocketAgentServerClient] 发送请求(非流式) E2A: request_id=%s channel=%s method=%s has_params=%s",
            rid,
            envelope.channel,
            envelope.method,
            bool(envelope.params),
        )

        if rid in self._message_queues:
            raise RuntimeError(
                f"WebSocketAgentServerClient: duplicate in-flight request_id={rid!r}; "
                "refusing to register queue (would mis-route responses, e.g. stream chunks to unary waiters)."
            )

        # 创建该请求的消息队列
        queue = asyncio.Queue()
        self._message_queues[rid] = queue

        try:
            # 发送请求
            async with self._lock:
                payload = _e2a_to_wire(envelope)
                logger.debug(
                    "[WebSocketAgentServerClient] 发送请求(非流式) wire: request_id=%s channel=%s method=%s payload_bytes=%d",
                    rid,
                    envelope.channel,
                    envelope.method,
                    len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")),
                )
                await self._ws.send(json.dumps(payload, ensure_ascii=False))

            try:
                data = await asyncio.wait_for(queue.get(), timeout=_UNARY_REQUEST_TIMEOUT_SECONDS)
                if isinstance(data, BaseException):
                    raise data
            except asyncio.TimeoutError as e:
                logger.warning(
                    "[WebSocketAgentServerClient] 非流式请求超时: request_id=%s timeout=%ss",
                    rid,
                    _UNARY_REQUEST_TIMEOUT_SECONDS,
                )
                raise RuntimeError(
                    f"AgentServer 非流式请求超时 (request_id={rid}, timeout={_UNARY_REQUEST_TIMEOUT_SECONDS}s)"
                ) from e
            logger.debug(
                "[WebSocketAgentServerClient] 收到响应(非流式) wire: request_id=%s kind=%s status=%s",
                data.get("request_id"),
                data.get("kind"),
                data.get("status"),
            )
            resp = parse_agent_server_wire_unary(data)
            logger.info(
                "[WebSocketAgentServerClient] 收到完整响应 AgentResponse: request_id=%s ok=%s has_payload=%s",
                resp.request_id,
                resp.ok,
                bool(resp.payload),
            )
            return resp
        finally:
            # 清理队列
            await self._drain_and_remove_queue(rid)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        self._ensure_connected()
        envelope.is_stream = True
        rid = _wire_request_id_key(envelope.request_id)
        logger.info(
            "[E2A][out][stream] request_id=%s channel=%s method=%s is_stream=%s",
            rid,
            envelope.channel,
            envelope.method,
            envelope.is_stream,
        )
        logger.debug(
            "[WebSocketAgentServerClient] 发送请求(流式) E2A: request_id=%s channel=%s method=%s has_params=%s",
            rid,
            envelope.channel,
            envelope.method,
            bool(envelope.params),
        )

        if rid in self._message_queues:
            raise RuntimeError(
                f"WebSocketAgentServerClient: duplicate in-flight request_id={rid!r}; "
                "refusing to register queue (would mis-route responses, e.g. stream chunks to unary waiters)."
            )

        # 创建该请求的消息队列
        queue = asyncio.Queue()
        self._message_queues[rid] = queue

        try:
            # 发送请求
            async with self._lock:
                payload = _e2a_to_wire(envelope)
                logger.debug(
                    "[WebSocketAgentServerClient] 发送请求(流式) wire: request_id=%s channel=%s method=%s payload_bytes=%d",
                    rid,
                    envelope.channel,
                    envelope.method,
                    len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")),
                )
                await self._ws.send(json.dumps(payload, ensure_ascii=False))

            # 从队列中接收流式响应
            chunk_count = 0
            saw_complete = False
            while True:
                if saw_complete:
                    try:
                        data = await asyncio.wait_for(
                            queue.get(),
                            timeout=_STREAM_TRAILING_MESSAGE_GRACE_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        break
                else:
                    data = await queue.get()
                if isinstance(data, BaseException):
                    raise data
                logger.debug(
                    "[WebSocketAgentServerClient] 收到流式事件 wire: request_id=%s kind=%s status=%s",
                    data.get("request_id"),
                    data.get("kind"),
                    data.get("status"),
                )
                chunk = parse_agent_server_wire_chunk(data)
                chunk_count += 1
                logger.debug(
                    "[WebSocketAgentServerClient] 收到流式 chunk #%s AgentResponseChunk: request_id=%s is_complete=%s has_payload=%s",
                    chunk_count,
                    chunk.request_id,
                    chunk.is_complete,
                    bool(chunk.payload),
                )
                yield chunk
                if chunk.is_complete:
                    saw_complete = True
            logger.info("[WebSocketAgentServerClient] 流式响应结束: request_id=%s 共 %s 个 chunk", rid, chunk_count)
        except asyncio.CancelledError:
            logger.info("[WebSocketAgentServerClient] 流式接收被取消: request_id=%s", rid)
            raise
        finally:
            # 清理队列
            await self._drain_and_remove_queue(rid)

    async def _drain_and_remove_queue(self, rid: str) -> None:
        """清空队列中的残余消息并移除队列，同时标记 request_id 为已取消状态.

        标记为已取消后，后续到达的残余消息会被 _message_receiver_loop 静默丢弃。
        使用锁保护，确保操作的原子性。
        """
        async with self._queue_lock:
            queue = self._message_queues.get(rid)
            if queue is None:
                return
            # 1. 先标记为已取消，阻止后续消息进入队列
            self._cancelled_request_ids.add(rid)
            # 2. 删除队列注册
            del self._message_queues[rid]
            # 3. 清空队列中的残余消息（非阻塞）
            drained_count = 0
            while True:
                try:
                    queue.get_nowait()
                    drained_count += 1
                except asyncio.QueueEmpty:
                    break
            logger.debug(
                "[WebSocketAgentServerClient] 队列已清空并移除: request_id=%s 清理消息数=%d",
                rid,
                drained_count,
            )
            # 4. 异步延迟清理已取消标记（给 AgentServer 一点时间发送残余消息）
            asyncio.create_task(self._delayed_cleanup_cancelled_request_id(rid))

    async def _delayed_cleanup_cancelled_request_id(self, rid: str) -> None:
        """延迟清理已取消的 request_id 标记.

        等待一段时间后清理，确保 AgentServer 的残余消息能够被静默丢弃而不打印日志。
        """
        # 等待足够时间让 AgentServer 的残余消息被接收和丢弃
        await asyncio.sleep(2.0)  # 2秒应该足够
        async with self._queue_lock:
            self._cancelled_request_ids.discard(rid)
            logger.debug(
                "[WebSocketAgentServerClient] 已取消标记已清理: request_id=%s",
                rid,
            )

    # =========================================================================
    # 文件传输方法
    # =========================================================================

    async def file_transfer_start(
        self,
        params: FileTransferStartParams,
    ) -> dict[str, Any]:
        """发送 FILE_TRANSFER_START 消息.

        Args:
            params: 文件传输开始参数

        Returns:
            AgentServer 的响应
        """
        self._ensure_connected()
        request_id = f"ft_start_{params.transfer_id}"

        envelope = E2AEnvelope(
            request_id=request_id,
            channel=params.channel_id,
            method=FILE_TRANSFER_START,
            params={
                "event_type": FILE_TRANSFER_START,
                "transfer_id": params.transfer_id,
                "filename": params.filename,
                "file_size": params.file_size,
                "sha256": params.sha256,
                "total_chunks": params.total_chunks,
                "chunk_size": params.chunk_size,
                "mime_type": params.mime_type,
            },
            session_id=params.session_id,
            is_stream=False,
        )

        logger.info(
            "[WebSocketAgentServerClient] 发送 FILE_TRANSFER_START: transfer_id=%s filename=%s",
            params.transfer_id,
            params.filename,
        )

        resp = await self.send_request(envelope)
        result = resp.payload or {}
        result["success"] = resp.ok
        return result

    async def file_transfer_chunk(
        self,
        transfer_id: str,
        chunk_index: int,
        base64_data: str,
        chunk_size: int,
        channel_id: str = "",
    ) -> dict[str, Any]:
        """发送 FILE_TRANSFER_CHUNK 消息.

        Args:
            transfer_id: 传输ID
            chunk_index: 分片索引
            base64_data: Base64 编码的分片数据
            chunk_size: 分片大小
            channel_id: 频道ID

        Returns:
            AgentServer 的响应
        """
        self._ensure_connected()
        request_id = f"ft_chunk_{transfer_id}_{chunk_index}"

        envelope = E2AEnvelope(
            request_id=request_id,
            channel=channel_id,
            method=FILE_TRANSFER_CHUNK,
            params={
                "event_type": FILE_TRANSFER_CHUNK,
                "transfer_id": transfer_id,
                "chunk_index": chunk_index,
                "base64_data": base64_data,
                "chunk_size": chunk_size,
            },
            is_stream=False,
        )

        logger.debug(
            "[WebSocketAgentServerClient] 发送 FILE_TRANSFER_CHUNK: transfer_id=%s chunk=%d",
            transfer_id,
            chunk_index,
        )

        resp = await self.send_request(envelope)
        result = resp.payload or {}
        result["success"] = resp.ok
        return result

    async def file_transfer_complete(
        self,
        transfer_id: str,
        sha256: str,
        channel_id: str = "",
    ) -> dict[str, Any]:
        """发送 FILE_TRANSFER_COMPLETE 消息.

        Args:
            transfer_id: 传输ID
            sha256: 文件 SHA256 校验值
            channel_id: 频道ID

        Returns:
            AgentServer 的响应，包含最终文件路径
        """
        self._ensure_connected()
        request_id = f"ft_complete_{transfer_id}"

        envelope = E2AEnvelope(
            request_id=request_id,
            channel=channel_id,
            method=FILE_TRANSFER_COMPLETE,
            params={
                "event_type": FILE_TRANSFER_COMPLETE,
                "transfer_id": transfer_id,
                "sha256": sha256,
            },
            is_stream=False,
        )

        logger.info(
            "[WebSocketAgentServerClient] 发送 FILE_TRANSFER_COMPLETE: transfer_id=%s",
            transfer_id,
        )

        resp = await self.send_request(envelope)
        result = resp.payload or {}
        result["success"] = resp.ok
        return result

    async def send_file(
        self,
        file_path: str,
        session_id: str = "",
        channel_id: str = "",
    ) -> dict[str, Any]:
        """发送文件到 AgentServer（分片发送）.

        这是高级方法，封装了 start -> chunks -> complete 的完整流程。

        Args:
            file_path: 文件路径
            session_id: 会话ID
            channel_id: 频道ID

        Returns:
            发送结果，包含 AgentServer 返回的最终文件路径
        """
        from pathlib import Path
        import hashlib
        import base64
        import time

        path = Path(file_path)
        if not path.exists():
            return {
                "success": False,
                "error": f"file not found: {file_path}",
            }

        # 读取文件
        try:
            file_data = path.read_bytes()
        except Exception as e:
            return {
                "success": False,
                "error": f"read file failed: {e}",
            }

        file_size = len(file_data)
        filename = path.name
        sha256 = hashlib.sha256(file_data).hexdigest()
        chunk_size = 65536  # 64KB
        total_chunks = (file_size + chunk_size - 1) // chunk_size

        transfer_id = f"up_{int(time.time() * 1000)}_{hashlib.md5(filename.encode()).hexdigest()[:8]}"

        # 1. 发送 START
        start_params = FileTransferStartParams(
            transfer_id=transfer_id,
            filename=filename,
            file_size=file_size,
            sha256=sha256,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            session_id=session_id,
            channel_id=channel_id,
        )
        start_resp = await self.file_transfer_start(start_params)

        if not start_resp.get("accepted"):
            return {
                "success": False,
                "error": start_resp.get("error", "transfer start rejected"),
            }

        # 2. 发送分片
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, file_size)
            chunk_data = file_data[start:end]
            b64_data = base64.b64encode(chunk_data).decode("utf-8")

            chunk_resp = await self.file_transfer_chunk(
                transfer_id=transfer_id,
                chunk_index=i,
                base64_data=b64_data,
                chunk_size=len(chunk_data),
                channel_id=channel_id,
            )

            if not chunk_resp.get("accepted"):
                logger.warning(
                    "[WebSocketAgentServerClient] 分片发送失败: chunk=%d error=%s",
                    i,
                    chunk_resp.get("error"),
                )

        # 3. 发送 COMPLETE
        complete_resp = await self.file_transfer_complete(
            transfer_id=transfer_id,
            sha256=sha256,
            channel_id=channel_id,
        )

        return complete_resp


# ---------------------------------------------------------------------------
# Mock AgentServer（协议兼容，供示例或测试使用）
# ---------------------------------------------------------------------------


async def mock_agent_server_handler(ws: Any) -> None:
    """
    协议兼容的 Mock AgentServer：按 is_stream 回 E2AResponse 线 JSON（与生产 AgentServer 一致）。
    """
    import websockets

    from jiuwenclaw.e2a.wire_codec import (
        encode_agent_chunk_for_wire,
        encode_agent_response_for_wire,
    )

    try:
        while True:
            raw = await ws.recv()
            data = json.loads(raw)
            req_id = data.get("request_id", "")
            ch_id = data.get("channel") or data.get("channel_id", "")
            params = data.get("params", {})
            is_stream = data.get("is_stream", False)
            params_str = json.dumps(params, ensure_ascii=False) if isinstance(params, dict) else str(params)

            if is_stream:
                for i, part in enumerate(["流式-1 ", "流式-2 ", "流式-3(完)"]):
                    chunk = AgentResponseChunk(
                        request_id=req_id,
                        channel_id=ch_id,
                        payload={"content": part},
                        is_complete=i == 2,
                    )
                    wire = encode_agent_chunk_for_wire(
                        chunk, response_id=req_id, sequence=i
                    )
                    await ws.send(json.dumps(wire, ensure_ascii=False))
            else:
                meta = data.get("metadata") or data.get("channel_context")
                if meta is not None and not isinstance(meta, dict):
                    meta = None
                resp = AgentResponse(
                    request_id=req_id,
                    channel_id=ch_id,
                    ok=True,
                    payload={"content": f"Echo: {params_str}"},
                    metadata=dict(meta) if isinstance(meta, dict) else None,
                )
                wire = encode_agent_response_for_wire(resp, response_id=req_id)
                await ws.send(json.dumps(wire, ensure_ascii=False))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.exception("[MockAgentServer] 处理异常: %s", e)


async def run_mock_agent_server(
    host: str = "127.0.0.1",
    port: int = 8000,
) -> Any:
    """
    启动 Mock AgentServer（使用 mock_agent_server_handler），监听 host:port。
    返回 Server，调用方需在结束时 server.close(); await server.wait_closed()。
    websockets 14+ 使用 legacy.server.serve，与 legacy 客户端一致，避免 InvalidMessage。
    """
    try:
        from websockets.legacy.server import serve as legacy_serve
        server = await legacy_serve(mock_agent_server_handler, host, port)
    except ImportError:
        import websockets
        server = await websockets.serve(mock_agent_server_handler, host, port)
    logger.info("[MockAgentServer] 已启动: ws://%s:%s", host, port)
    return server


# ---------------------------------------------------------------------------
# 自验证：内存 Mock 服务端 + main
# ---------------------------------------------------------------------------


async def _run_verification() -> None:
    """用内存 Mock 服务端验证 WebSocketAgentServerClient 的 connect/send_request/send_request_stream."""
    from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields

    port = 18765
    uri = f"ws://127.0.0.1:{port}"
    server = await run_mock_agent_server("127.0.0.1", port)
    logger.info("[main] Mock AgentServer 已启动: %s", uri)

    client = WebSocketAgentServerClient()
    try:
        await client.connect(uri)

        # 1. 非流式请求
        req1 = e2a_from_agent_fields(
            request_id="req-1",
            channel_id="ch-1",
            session_id="sess-1",
            params={"message": "你好"},
        )
        resp1 = await client.send_request(req1)
        assert resp1.request_id == "req-1"
        assert resp1.ok is True
        assert "Echo:" in str(resp1.payload)
        logger.info("[main] 非流式验证通过: request_id=%s ok=%s", resp1.request_id, resp1.ok)

        # 2. 流式请求
        req2 = e2a_from_agent_fields(
            request_id="req-2",
            channel_id="ch-1",
            session_id="sess-1",
            params={"message": "流式测试"},
        )
        chunks = []
        async for ch in client.send_request_stream(req2):
            chunks.append(ch)
        assert len(chunks) == 3
        assert chunks[-1].is_complete
        logger.info("[main] 流式验证通过: 共 %s 个 chunk", len(chunks))
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
    logger.info("[main] 验证完成，功能正常")


def main() -> None:
    """入口：配置日志并运行自验证."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(process)d] %(levelname)s %(name)s %(filename)s:%(lineno)d: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(_run_verification())


if __name__ == "__main__":
    main()
