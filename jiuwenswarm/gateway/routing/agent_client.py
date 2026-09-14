# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServerClient - Gateway 与 AgentServer 的 WebSocket 客户端.

桌面形态（stdin 密钥包携带 ``pipes.agentE2a``）：WebSocketAgentServerClient
的连接载体自动切换为命名管道（open_pipe + auth 首帧 + 等 connection.ack），
接收循环/断连重连语义与 WS 形态一致（PipeError/FrameCodecError ↔
ConnectionClosed）；无密钥包时回退 ws:// 现状（行为零变化）。
"""

from __future__ import annotations

import logging
import asyncio
import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed, PayloadTooBig

from jiuwenswarm.common.np_transport import FrameCodecError, PipeClosedError, PipeError

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)


logger = logging.getLogger(__name__)
_STREAM_TRAILING_MESSAGE_GRACE_SECONDS = 0.7
AGENT_REQUEST_TIMEOUT_SECONDS: float = 600.0
_UNARY_REQUEST_TIMEOUT_SECONDS = AGENT_REQUEST_TIMEOUT_SECONDS
# 流式响应空闲上限（无任何 chunk 的最长等待）：与一元请求同级 600s，
# 防止 AgentServer 无产出时 gateway 转发循环被裸 queue.get() 永久堵住
_STREAM_IDLE_TIMEOUT_SECONDS: float = 600.0


class AgentServerUnaryTimeout(RuntimeError):
    """非流式请求等待 AgentServer 响应超时。

    继承 ``RuntimeError`` 以保持向后兼容：调用方此前依赖 ``RuntimeError`` 与
    "AgentServer 非流式请求超时" 文案。作为独立类型后，cron 等显式传入
    ``timeout`` 的调用方能精确捕获并执行自身的超时收尾（如 cancel 后端
    会话），而不是被写死的内层 600s 截断成裸 ``RuntimeError``、跳过收尾。
    """

    def __init__(self, request_id: str, timeout: float) -> None:
        super().__init__(
            f"AgentServer 非流式请求超时 (request_id={request_id}, timeout={timeout}s)"
        )
        self.request_id = request_id
        self.timeout = timeout


class _ReceiverFailure:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


def _wire_request_id_key(request_id: Any) -> str:
    """与 AgentServer 回包 ``request_id`` 对齐：统一为 str，避免 JSON 数字/字符串导致队列键不一致。"""
    if request_id is None:
        return ""
    return str(request_id)


def _to_json(data: Any) -> str:
    """将任意对象序列化为日志友好的 JSON 字符串."""
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(data)


def _payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    """隐藏上传正文，避免 Base64 日志造成巨量 I/O 并泄露文件内容。"""
    params = payload.get("params")
    if not isinstance(params, dict) or "file_content" not in params:
        return payload
    content = params.get("file_content")
    safe = dict(payload)
    safe["params"] = dict(params)
    safe["params"]["file_content"] = (
        f"<omitted base64 chars={len(content)}>"
        if isinstance(content, str)
        else "<omitted>"
    )
    return safe


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


def resolve_agent_e2a_pipe_path() -> str | None:
    """桌面 E2A stdio 形态下 Gateway→AgentServer 的命名管道路径（密钥包
    ``pipes.agentE2a``）。

    形态判定跟随 AgentServer 侧（``e2aTransport == 'stdio'``，而非密钥包存在）：
    只有 stdio 形态下 AgentServer 才停开 TCP 18592 并起 agent-e2a 管道 server；
    WS 形态（含迁移期默认）gateway 必须回退 ``AGENT_SERVER_URL`` 的 ws:// 现状。
    非 Windows/密钥包未携带管道路径时同样返回 None。判定失败按 WS 形态处理。
    """
    if sys.platform != "win32":
        return None
    try:
        from jiuwenswarm.common.secrets_bootstrap import get_secret
        from jiuwenswarm.server.e2a_transports import e2a_stdio_mode_enabled

        if not e2a_stdio_mode_enabled():
            return None
        path = str(get_secret("pipes.agentE2a", "") or "").strip()
    except Exception:  # noqa: BLE001 - 防御：模块不可用时按 WS 形态
        return None
    return path or None


class _PipeWsAdapter:
    """PipeMessageTransport 的 ws 皮（recv/send/close 鸭子形态）。

    让命名管道连接直接接入 WebSocketAgentServerClient 既有的接收循环
    （``self._ws.recv()``）与发送路径（``self._ws.send(str)``）：
    - recv：管道对端干净关闭（recv_text None）转为 PipeClosedError——由接收循环
      的 (ConnectionClosed, PipeError, FrameCodecError) 捕获组按断连处理；
    - send：帧文本直写长度前缀帧（与 WS 文本帧同负载）。
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport
        self.remote_address = transport.remote_address

    @property
    def closed(self) -> bool:
        return self._transport.closed

    async def recv(self) -> str:
        text = await self._transport.recv_text()
        if text is None:
            raise PipeClosedError("AgentServer 命名管道已关闭")
        return text

    async def send(self, data: str) -> None:
        await self._transport.send_text(data)

    async def close(self) -> None:
        await self._transport.close()


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
    async def send_request(
        self,
        envelope: E2AEnvelope,
        *,
        timeout: float | None = None,
    ) -> AgentResponse:
        """发送 E2A 信封，等待完整响应.

        Args:
            envelope: E2A 信封.
            timeout: 等待响应的上限（秒）。``None`` 时使用客户端默认值
                （``_UNARY_REQUEST_TIMEOUT_SECONDS``，600s）。调用方可传入
                更大的值以覆盖默认上限（例如 cron 任务的 ``timeout_seconds``），
                使任务自身的超时真正生效，而非被内层默认值提前截断.
        """
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
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._server_ready: bool = False
        # 消息分发机制：根据 request_id 路由到对应队列
        self._message_queues: dict[str, asyncio.Queue] = {}
        self._queue_lock = asyncio.Lock()  # 保护队列操作的锁
        self._cancelled_request_ids: set[str] = set()  # 已取消但等待清理的 request_id
        self._receiver_task: asyncio.Task | None = None
        self._running = False
        # AgentServer send_push：旁路投递，勿进入与 request_id 绑定的 RPC 等待队列
        self._on_server_push: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._server_push_tasks: set[asyncio.Task[Any]] = set()
        self._on_disconnect: Callable[[BaseException | None], Awaitable[None]] | None = None
        self._disconnect_notified = False

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        """注册 Agent 主动推送处理回调（metadata 含 ``E2A_WIRE_SERVER_PUSH_KEY`` 的帧）。"""
        self._on_server_push = handler

    def set_disconnect_handler(
        self,
        handler: Callable[[BaseException | None], Awaitable[None]] | None,
    ) -> None:
        """Register a process-local transport-disconnect lifecycle callback."""
        self._on_disconnect = handler

    def _diagnostic_state(self, ws: Any | None = None) -> dict[str, Any]:
        target_ws = self._ws if ws is None else ws
        return {
            "uri": self._uri,
            "running": self._running,
            "server_ready": self._server_ready,
            "pending_requests": len(self._message_queues),
            "cancelled_requests": len(self._cancelled_request_ids),
            "ping_interval": self._ping_interval,
            "ping_timeout": self._ping_timeout,
            **describe_ws_peer(target_ws),
        }

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
        if self._ws is not None:
            await self.disconnect()
        self._uri = uri
        self._server_ready = False
        self._disconnect_notified = False
        pipe_path = resolve_agent_e2a_pipe_path()
        if pipe_path is not None:
            # 桌面形态：命名管道（open_pipe 自带服务端未起时的等待重试）+ auth 首帧
            logger.info("[WebSocketAgentServerClient] 正在连接(命名管道): %s", pipe_path)
            self._ws = await self._open_pipe_connection(pipe_path)
            logger.info("[WebSocketAgentServerClient] 已连接(命名管道): %s", pipe_path)
        else:
            logger.info("[WebSocketAgentServerClient] 正在连接: %s", uri)
            origin = _build_ws_origin(uri)
            try:
                from websockets.legacy.client import connect as legacy_connect
                connect_fn = legacy_connect
            except ImportError:
                import websockets
                connect_fn = websockets.connect
            self._ws = await connect_fn(
                uri,
                origin=origin,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                close_timeout=5.0,
                max_size=AGENT_WS_MAX_MESSAGE_BYTES,
            )
            logger.info("[WebSocketAgentServerClient] 已连接: %s", uri)

        # 读取 AgentServer 的 connection.ack 事件
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
            logger.info("[WebSocketAgentServerClient] connect 首帧(raw): %s", raw)
            data = json.loads(raw)
            logger.info("[WebSocketAgentServerClient] connect 首帧(parsed): %s", _to_json(data))
            if data.get("type") == "event" and data.get("event") == "connection.ack":
                self._server_ready = True
                logger.info("[WebSocketAgentServerClient] 收到 connection.ack，AgentServer 已就绪")
            else:
                logger.warning(
                    "[WebSocketAgentServerClient] 首帧非 connection.ack: %s",
                    data.get("type"),
                )
        except asyncio.TimeoutError:
            logger.warning("[WebSocketAgentServerClient] 等待 connection.ack 超时")
        except Exception as e:
            logger.warning("[WebSocketAgentServerClient] 读取 connection.ack 失败: %s", e)

        # 启动消息接收和分发任务
        self._running = True
        self._receiver_task = asyncio.create_task(self._message_receiver_loop())
        logger.info("[WebSocketAgentServerClient] 消息接收任务已启动")

    async def _open_pipe_connection(self, pipe_path: str) -> _PipeWsAdapter:
        """桌面形态建连：open_pipe → auth 首帧（e2aToken，服务端校验失败即断管）。"""
        from jiuwenswarm.common.np_transport import open_pipe
        from jiuwenswarm.common.secrets_bootstrap import get_secret
        from jiuwenswarm.server.e2a_transports import PipeMessageTransport

        stream = await open_pipe(pipe_path, timeout=10.0)
        try:
            token = str(get_secret("e2aToken", "") or "")
            await stream.send_frame({"type": "auth", "token": token})
        except Exception:
            await stream.close()
            raise
        return _PipeWsAdapter(PipeMessageTransport(stream))

    async def _message_receiver_loop(self) -> None:
        """后台任务：从 WebSocket 接收消息并根据 request_id 分发到对应队列."""
        try:
            while self._running and self._ws is not None:
                try:
                    raw = await self._ws.recv()
                    data = json.loads(raw)
                    meta = data.get("metadata")
                    if isinstance(meta, dict) and meta.get(E2A_WIRE_SERVER_PUSH_KEY):
                        response_kind = str(data.get("response_kind") or "")
                        if response_kind.startswith("xiaoyi.gui_rpc."):
                            body = data.get("body")
                            rpc_id = (
                                str(body.get("rpc_id") or "")
                                if isinstance(body, dict)
                                else ""
                            )
                            logger.info(
                                "[GUI_RPC_TRACE] phase=GATEWAY_WS_PUSH_RECEIVED "
                                "rpc_id=%s response_kind=%s handler_registered=%s",
                                rpc_id,
                                response_kind,
                                self._on_server_push is not None,
                            )
                        if self._on_server_push is not None:
                            task = asyncio.create_task(self._on_server_push(data))
                            self._server_push_tasks.add(task)
                            task.add_done_callback(self._server_push_tasks.discard)
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
                        # 检查是否是已取消的请求，静默丢弃消息。
                        # 不放宽为"rid 已有新 queue 就放行"——_drain_and_remove_queue
                        # 删 queue 后会保留 _cancelled_request_ids 标记 2s，若在此窗口内
                        # 新请求复用同 rid 并建新 queue，放行旧请求的残余响应会串进新请求
                        # 的 queue，导致新请求拿到错误响应（响应串线，比超时更危险）。
                        # send_request 已有 "duplicate in-flight request_id" 防护（402行），
                        # 正常路径下 queue 不被提前删；此处 2s 丢弃窗口仅针对已取消的残余。
                        if request_id in self._cancelled_request_ids:
                            logger.debug(
                                "[WebSocketAgentServerClient] 收到已取消请求的残余消息，已丢弃: request_id=%s",
                                request_id
                            )
                            continue

                        if request_id and request_id in self._message_queues:
                            await self._message_queues[request_id].put(data)
                        else:
                            # 没有对应的队列（非预期情况）——可能是 E2A 编解码导致
                            # request_id 不匹配，此时 send_request 会等满自身超时
                            # （默认 600s，或调用方透传的 timeout）。
                            # 提升为 warning 让问题可见，便于排查"到点没推送"类故障。
                            logger.warning(
                                "[WebSocketAgentServerClient] 收到无目标队列的消息（等待方将超时）: request_id=%s",
                                request_id
                            )
                except asyncio.CancelledError:
                    break
                except PayloadTooBig as e:
                    logger.error(
                        "[WebSocketAgentServerClient] AgentServer 消息超过 WebSocket 限制: %s",
                        format_ws_diagnostics(
                            self._diagnostic_state(),
                            describe_ws_exception(e),
                        ),
                    )
                    await self._stop_receiver_after_fatal_error(e)
                    break
                except (ConnectionClosed, PipeError, FrameCodecError) as e:
                    logger.info(
                        "[WebSocketAgentServerClient] AgentServer WebSocket 已关闭: %s",
                        format_ws_diagnostics(
                            self._diagnostic_state(),
                            describe_ws_exception(e),
                        ),
                    )
                    await self._stop_receiver_after_fatal_error(e)
                    break
                except Exception as e:
                    logger.exception(
                        "[WebSocketAgentServerClient] 消息接收循环异常: %s",
                        format_ws_diagnostics(
                            self._diagnostic_state(),
                            describe_ws_exception(e),
                        ),
                    )
                    if (
                        not self._running
                        or self._ws is None
                        or type(e).__name__.startswith("ConnectionClosed")
                    ):
                        self._running = False
                        break
                    await asyncio.sleep(0.1)  # 避免快速循环
        finally:
            self._server_ready = False
            await self._cancel_server_push_tasks()
            logger.info("[WebSocketAgentServerClient] 消息接收任务已停止")

    async def _stop_receiver_after_fatal_error(self, exc: BaseException) -> None:
        detail = format_ws_diagnostics(
            self._diagnostic_state(),
            describe_ws_exception(exc),
        )
        self._running = False
        self._server_ready = False
        ws = self._ws
        self._ws = None
        failure = _ReceiverFailure(exc)
        async with self._queue_lock:
            for queue in self._message_queues.values():
                queue.put_nowait(failure)
        await self._notify_disconnect(exc)
        # 收尾关闭传输（管道形态避免句柄泄漏累积；WS 形态对已关闭连接是 no-op）
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - 断连收尾容错
                pass
        logger.info("[WebSocketAgentServerClient] 接收任务已停止并通知等待队列: %s", detail)

    async def disconnect(self) -> None:
        # 停止接收任务
        self._running = False
        await self._notify_disconnect(None)
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None

        # 清理所有队列
        self._message_queues.clear()

        # 关闭 WebSocket
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception as e:
            logger.warning(
                "关闭 AgentServer WebSocket 时异常: %s",
                format_ws_diagnostics(
                    self._diagnostic_state(),
                    describe_ws_exception(e),
                ),
            )
        finally:
            self._ws = None
            self._uri = None
        logger.info("[WebSocketAgentServerClient] 已断开")

    async def _notify_disconnect(self, exc: BaseException | None) -> None:
        if self._disconnect_notified:
            return
        self._disconnect_notified = True
        handler = self._on_disconnect
        if handler is None:
            return
        try:
            await handler(exc)
        except Exception:
            logger.exception(
                "[WebSocketAgentServerClient] disconnect handler failed"
            )

    async def _cancel_server_push_tasks(self) -> None:
        tasks = [
            task
            for task in self._server_push_tasks
            if not task.done() and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._server_push_tasks.clear()

    def _ensure_connected(self) -> None:
        if self._ws is None:
            raise RuntimeError("未连接 AgentServer，请先调用 connect(uri)")

    async def _ensure_connected_for_request(self) -> None:
        if self._ws is not None:
            return
        uri = self._uri
        if not uri:
            raise RuntimeError("未连接 AgentServer，请先调用 connect(uri)")
        async with self._reconnect_lock:
            if self._ws is not None:
                return
            logger.info(
                "[WebSocketAgentServerClient] WebSocket 已断开，准备按需重连: %s",
                format_ws_diagnostics(self._diagnostic_state(), uri=uri),
            )
            await self.connect(uri)

    async def _send_wire_payload(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("未连接 AgentServer，请先调用 connect(uri)")
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except (ConnectionClosed, OSError, PipeError, FrameCodecError) as exc:
            logger.info(
                "[WebSocketAgentServerClient] AgentServer WebSocket 发送失败，连接将重置: %s",
                format_ws_diagnostics(
                    self._diagnostic_state(ws),
                    describe_ws_exception(exc),
                    request_id=_wire_request_id_key(payload.get("request_id")),
                    channel=payload.get("channel"),
                    method=payload.get("method"),
                ),
            )
            await self._stop_receiver_after_fatal_error(exc)
            raise RuntimeError("AgentServer WebSocket connection closed") from exc

    async def send_request(
        self,
        envelope: E2AEnvelope,
        *,
        timeout: float | None = None,
    ) -> AgentResponse:
        await self._ensure_connected_for_request()
        # 非流式 API 必须与 AgentServer 的 unary 路径一致；忽略信封上误带的 is_stream=True。
        envelope.is_stream = False
        rid = _wire_request_id_key(envelope.request_id)
        # 调用方可显式覆盖等待上限（如 cron 任务的 timeout_seconds）；未指定时
        # 回退到客户端默认 600s。这样任务自身的超时真正生效，而非被内层默认值
        # 提前截断成裸 RuntimeError、跳过调用方的超时收尾（cancel 后端会话等）。
        effective_timeout = (
            float(timeout) if timeout is not None else _UNARY_REQUEST_TIMEOUT_SECONDS
        )
        logger.info(
            "[E2A][out][nostream] request_id=%s channel=%s method=%s is_stream=%s",
            rid,
            envelope.channel,
            envelope.method,
            envelope.is_stream,
        )
        is_gui_rpc_response = envelope.method == "xiaoyi.gui_rpc.response"
        if is_gui_rpc_response:
            logger.debug(
                "[WebSocketAgentServerClient] 发送 GUI RPC 响应: request_id=%s",
                rid,
            )
        else:
            logger.debug(
                "[WebSocketAgentServerClient] 发送请求(非流式) E2A: %s",
                _to_json(envelope.to_dict()),
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
                if is_gui_rpc_response:
                    logger.info(
                        "[WebSocketAgentServerClient] 发送 GUI RPC 响应: request_id=%s",
                        rid,
                    )
                else:
                    logger.info(
                        "[WebSocketAgentServerClient] 发送请求(非流式) payload: %s",
                        _to_json(_payload_for_log(payload)),
                    )
                await self._send_wire_payload(payload)

            try:
                data = await asyncio.wait_for(queue.get(), timeout=effective_timeout)
                if isinstance(data, _ReceiverFailure):
                    raise RuntimeError("AgentServer WebSocket connection closed") from data.exc
            except asyncio.TimeoutError as e:
                logger.warning(
                    "[WebSocketAgentServerClient] 非流式请求超时: request_id=%s timeout=%ss",
                    rid,
                    effective_timeout,
                )
                raise AgentServerUnaryTimeout(rid, effective_timeout) from e
            resp = parse_agent_server_wire_unary(data)
            return resp
        finally:
            # 清理队列
            await self._drain_and_remove_queue(rid)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        await self._ensure_connected_for_request()
        envelope.is_stream = True
        rid = _wire_request_id_key(envelope.request_id)
        is_xiaoyi_request = str(envelope.channel or "").strip().lower() == "xiaoyi"
        if is_xiaoyi_request:
            logger.info(
                "[GUI_AGENT_DIAG] phase=GATEWAY_AGENT_STREAM_BEGIN "
                "request_id=%s session_id=%s envelope=%r",
                rid,
                envelope.session_id,
                envelope.to_dict(),
            )
        logger.info(
            "[E2A][out][stream] request_id=%s channel=%s method=%s is_stream=%s",
            rid,
            envelope.channel,
            envelope.method,
            envelope.is_stream,
        )
        logger.debug(
            "[WebSocketAgentServerClient] 发送请求(流式) E2A: %s",
            _to_json(envelope.to_dict()),
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
                logger.info(
                    "[WebSocketAgentServerClient] 发送请求(流式) payload: %s",
                    _to_json(_payload_for_log(payload)),
                )
                await self._send_wire_payload(payload)

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
                    # 首包/流中空闲必须有界：queue.get() 裸等时，一旦 AgentServer 侧
                    # 无产出（请求丢失/模型调用挂起），gateway 串行转发循环被永久
                    # 堵住——后续所有渠道消息只入队无回复（ws/link 无响应事故）。
                    try:
                        data = await asyncio.wait_for(
                            queue.get(),
                            timeout=_STREAM_IDLE_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError as idle_exc:
                        logger.warning(
                            "[WebSocketAgentServerClient] 流式响应空闲超时: request_id=%s "
                            "chunks=%d idle_timeout=%ss",
                            rid,
                            chunk_count,
                            _STREAM_IDLE_TIMEOUT_SECONDS,
                        )
                        raise RuntimeError(
                            f"AgentServer 流式响应空闲超时 (request_id={rid}, "
                            f"idle_timeout={_STREAM_IDLE_TIMEOUT_SECONDS}s)"
                        ) from idle_exc
                if isinstance(data, _ReceiverFailure):
                    raise RuntimeError("AgentServer WebSocket connection closed") from data.exc
                chunk = parse_agent_server_wire_chunk(data)
                chunk_count += 1
                if chunk_count <= 3:
                    _pl = getattr(chunk, "payload", None) or {}
                    _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                    logger.info(
                        "[WebSocketAgentServerClient] stream chunk received:"
                        " request_id=%s seq=%s event_type=%s",
                        rid, chunk_count, _et,
                    )
                if is_xiaoyi_request:
                    logger.info(
                        "[GUI_AGENT_DIAG] phase=GATEWAY_AGENT_CHUNK_RECEIVED "
                        "request_id=%s sequence=%s event_type=%s "
                        "is_complete=%s payload=%r wire=%r",
                        rid,
                        chunk_count - 1,
                        (
                            chunk.payload.get("event_type")
                            if isinstance(chunk.payload, dict)
                            else None
                        ),
                        chunk.is_complete,
                        chunk.payload,
                        data,
                    )
                yield chunk
                if chunk.is_complete:
                    saw_complete = True
            if is_xiaoyi_request:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=GATEWAY_AGENT_STREAM_END "
                    "request_id=%s chunk_count=%s saw_complete=%s",
                    rid,
                    chunk_count,
                    saw_complete,
                )
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


# ---------------------------------------------------------------------------
# Mock AgentServer（协议兼容，供示例或测试使用）
# ---------------------------------------------------------------------------


async def mock_agent_server_handler(ws: Any) -> None:
    """
    协议兼容的 Mock AgentServer：按 is_stream 回 E2AResponse 线 JSON（与生产 AgentServer 一致）。
    """
    import websockets

    from jiuwenswarm.common.e2a.wire_codec import (
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
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

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
        logger.info("[main] 非流式验证通过: payload=%s", resp1.payload)

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
        full_content = "".join(c.payload.get("content", "") for c in chunks if c.payload)
        logger.info("[main] 流式验证通过: 共 %s 个 chunk, 拼接内容=%r", len(chunks), full_content)
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
    logger.info("[main] 验证完成，功能正常")


def main() -> None:
    """入口：配置日志并运行自验证."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(_run_verification())


if __name__ == "__main__":
    main()
