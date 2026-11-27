# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""OpenAbility WebSocket 客户端：Gateway 经 OA 转发至沙箱内 AgentServer。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - compatibility fallback
    ConnectionClosed = Exception  # type: ignore[assignment]

from jiuwenclaw.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenclaw.gateway.agent_client import AgentServerClient
from jiuwenclaw.gateway.open_ability_wire import (
    open_ability_connect_headers,
    parse_openability_inbound_message,
    wrap_openability_message,
)
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenclaw.log import interface_info


logger = logging.getLogger(__name__)
_LOG_LABEL = "[OpenAbilityWebSocketClient]"
_STREAM_TRAILING_MESSAGE_GRACE_SECONDS = 0.7
_STREAM_REORDER_GAP_TIMEOUT_SECONDS = 2.0
_STREAM_REORDER_MAX_PENDING_FRAMES = 1024
_DEFAULT_UNARY_REQUEST_TIMEOUT_SECONDS = 600.0
_WS_MAX_SIZE = 8 * 2**20
_OA_INBOUND_DELTA_PREVIEW_CHARS = 200


def _wire_request_id_key(request_id: Any) -> str:
    if request_id is None:
        return ""
    return str(request_id)


def _e2a_to_wire(envelope: E2AEnvelope) -> dict[str, Any]:
    return envelope.to_dict()


def _build_ws_origin(uri: str) -> str | None:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.netloc}"


def _log_oa_inbound_frame(
    data: dict[str, Any],
    *,
    session_id: str,
    sandbox_id: str,
) -> None:
    """Log the decoded OA frame before Gateway routing or normalization."""
    if not logger.isEnabledFor(logging.INFO):
        return

    body = data.get("body")
    body = body if isinstance(body, dict) else {}
    delta = body.get("delta")
    if isinstance(delta, str):
        delta_preview = delta[:_OA_INBOUND_DELTA_PREVIEW_CHARS]
    elif delta is None:
        delta_preview = ""
    else:
        rendered_delta = json.dumps(delta, ensure_ascii=False, default=str)
        delta_preview = rendered_delta[:_OA_INBOUND_DELTA_PREVIEW_CHARS]

    logger.info(
        "[STREAM_DIAG][OA_IN] session_id=%s sandbox_id=%s request_id=%s delta_preview=%r",
        session_id,
        sandbox_id,
        data.get("request_id"),
        delta_preview,
    )


def _stream_frame_sequence(data: dict[str, Any]) -> int | None:
    sequence = data.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return sequence


def _is_terminal_stream_frame(data: dict[str, Any]) -> bool:
    return data.get("is_final") is True or data.get("is_complete") is True


class _StreamFrameReorderBuffer:
    """Restore the per-request E2A sequence before wire normalization."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        session_id: str,
        request_id: str,
        gap_timeout_seconds: float,
        max_pending_frames: int,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._session_id = session_id
        self._request_id = request_id
        self._gap_timeout_seconds = gap_timeout_seconds
        self._max_pending_frames = max_pending_frames
        self._expected_sequence = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._gap_started_at: float | None = None

    @property
    def has_gap(self) -> bool:
        return bool(self._pending)

    def gap_timeout_remaining(self, now: float) -> float:
        if self._gap_started_at is None:
            return self._gap_timeout_seconds
        return max(
            0.0,
            self._gap_timeout_seconds - (now - self._gap_started_at),
        )

    def offer(self, data: dict[str, Any], now: float) -> list[dict[str, Any]]:
        sequence = _stream_frame_sequence(data)
        if sequence is None:
            logger.warning(
                "[STREAM_REORDER] invalid sequence; pass through: "
                "session_id=%s sandbox_id=%s request_id=%s sequence=%r",
                self._session_id,
                self._sandbox_id,
                self._request_id,
                data.get("sequence"),
            )
            return [data]

        # AgentServer keepalive frames use -1 and do not consume a stream sequence.
        if sequence == -1:
            return [data]
        if sequence < -1:
            logger.warning(
                "[STREAM_REORDER] invalid negative sequence; pass through: "
                "session_id=%s sandbox_id=%s request_id=%s sequence=%s",
                self._session_id,
                self._sandbox_id,
                self._request_id,
                sequence,
            )
            return [data]

        if sequence < self._expected_sequence:
            logger.warning(
                "[STREAM_REORDER] drop duplicate/late frame: "
                "session_id=%s sandbox_id=%s request_id=%s expected_seq=%s received_seq=%s",
                self._session_id,
                self._sandbox_id,
                self._request_id,
                self._expected_sequence,
                sequence,
            )
            return []

        if sequence == self._expected_sequence:
            ready = [data]
            self._expected_sequence += 1
            while self._expected_sequence in self._pending:
                ready.append(self._pending.pop(self._expected_sequence))
                self._expected_sequence += 1
            self._gap_started_at = now if self._pending else None
            return ready

        if sequence in self._pending:
            logger.warning(
                "[STREAM_REORDER] drop duplicate buffered frame: "
                "session_id=%s sandbox_id=%s request_id=%s expected_seq=%s received_seq=%s",
                self._session_id,
                self._sandbox_id,
                self._request_id,
                self._expected_sequence,
                sequence,
            )
            return []

        self._pending[sequence] = data
        if self._gap_started_at is None:
            self._gap_started_at = now
        logger.warning(
            "[STREAM_REORDER] buffer out-of-order frame: "
            "session_id=%s sandbox_id=%s request_id=%s expected_seq=%s "
            "received_seq=%s pending=%s",
            self._session_id,
            self._sandbox_id,
            self._request_id,
            self._expected_sequence,
            sequence,
            len(self._pending),
        )
        if len(self._pending) >= self._max_pending_frames:
            return self.flush(reason="buffer_limit")
        return []

    def flush(self, *, reason: str) -> list[dict[str, Any]]:
        if not self._pending:
            self._gap_started_at = None
            return []

        sequences = sorted(self._pending)
        ready = [self._pending[sequence] for sequence in sequences]
        logger.warning(
            "[STREAM_REORDER] flush with sequence gap: "
            "session_id=%s sandbox_id=%s request_id=%s reason=%s "
            "expected_seq=%s first_buffered_seq=%s last_buffered_seq=%s pending=%s",
            self._session_id,
            self._sandbox_id,
            self._request_id,
            reason,
            self._expected_sequence,
            sequences[0],
            sequences[-1],
            len(sequences),
        )
        self._expected_sequence = sequences[-1] + 1
        self._pending.clear()
        self._gap_started_at = None
        return ready


async def _iter_reordered_stream_frames(
    queue: asyncio.Queue,
    *,
    sandbox_id: str,
    session_id: str,
    request_id: str,
    gap_timeout_seconds: float = _STREAM_REORDER_GAP_TIMEOUT_SECONDS,
    max_pending_frames: int = _STREAM_REORDER_MAX_PENDING_FRAMES,
    trailing_grace_seconds: float = _STREAM_TRAILING_MESSAGE_GRACE_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    reorder = _StreamFrameReorderBuffer(
        sandbox_id=sandbox_id,
        session_id=session_id,
        request_id=request_id,
        gap_timeout_seconds=gap_timeout_seconds,
        max_pending_frames=max_pending_frames,
    )
    loop = asyncio.get_running_loop()
    saw_complete = False

    while True:
        timeout: float | None = None
        if saw_complete:
            timeout = trailing_grace_seconds
        elif reorder.has_gap:
            timeout = reorder.gap_timeout_remaining(loop.time())

        try:
            if timeout is None:
                data = await queue.get()
            else:
                data = await asyncio.wait_for(queue.get(), timeout=timeout)
            ready = reorder.offer(data, loop.time())
        except asyncio.TimeoutError:
            if saw_complete:
                break
            ready = reorder.flush(reason="gap_timeout")

        for frame in ready:
            yield frame
            if _is_terminal_stream_frame(frame):
                saw_complete = True


class OpenAbilityWebSocketClient(AgentServerClient):
    """
    与 OpenAbility 的 WebSocket 客户端（独立实现，不修改 agent_client.py）。

    出站外层帧：``{"sandboxId": "...", "msgDetail": "<E2A JSON string>"}``；
    入站为裸 E2A JSON 字符串（与 ``msgDetail`` 字段值一致），接收后解析为 dict。
    建连时携带 ``x-hag-trace-id: jiuwen-gateway``；建连后无 ``connection.ack``，即可收发。
    """

    def __init__(
        self,
        sandbox_id: str,
        *,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
        request_timeout_seconds: float = _DEFAULT_UNARY_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        sid = str(sandbox_id or "").strip()
        if not sid:
            raise ValueError("OpenAbilityWebSocketClient: sandbox_id is required")
        self._sandbox_id = sid
        self._uri: str | None = None
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._request_timeout_seconds = float(
            request_timeout_seconds
            if request_timeout_seconds and request_timeout_seconds > 0
            else _DEFAULT_UNARY_REQUEST_TIMEOUT_SECONDS
        )
        self._server_ready = False
        self._message_queues: dict[str, asyncio.Queue] = {}
        self._request_session_ids: dict[str, str] = {}
        self._queue_lock = asyncio.Lock()
        self._cancelled_request_ids: set[str] = set()
        self._receiver_task: asyncio.Task | None = None
        self._running = False
        self._on_server_push: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._on_connection_lost: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._on_link_heartbeat: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @property
    def server_ready(self) -> bool:
        return self._server_ready

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        self._on_server_push = handler

    def set_connection_lost_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        self._on_connection_lost = handler

    def set_link_heartbeat_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        self._on_link_heartbeat = handler

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    def _encode_outbound(self, detail: dict[str, Any]) -> str:
        outer = wrap_openability_message(self._sandbox_id, detail)
        return json.dumps(outer, ensure_ascii=False)

    def _decode_inbound(self, frame: Any) -> dict[str, Any]:
        return parse_openability_inbound_message(frame)

    async def connect(self, uri: str) -> None:
        if self._ws is not None:
            await self.disconnect()
        logger.info("%s 正在连接: %s sandbox_id=%s", _LOG_LABEL, uri, self._sandbox_id)
        self._uri = uri
        origin = _build_ws_origin(uri)
        trace_headers = open_ability_connect_headers()
        try:
            from websockets.legacy.client import connect as legacy_connect

            connect_fn = legacy_connect
            header_kwarg = {"extra_headers": trace_headers}
        except ImportError:
            import websockets

            connect_fn = websockets.connect
            header_kwarg = {"additional_headers": trace_headers}
        self._ws = await connect_fn(
            uri,
            origin=origin,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            close_timeout=5.0,
            max_size=_WS_MAX_SIZE,
            **header_kwarg,
        )
        self._server_ready = True
        logger.info("%s 已连接（无 connection.ack）: %s", _LOG_LABEL, uri)
        self._running = True
        self._receiver_task = asyncio.create_task(self._message_receiver_loop())
        logger.info("%s 消息接收任务已启动", _LOG_LABEL)

    async def disconnect(self) -> None:
        self._running = False
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None
        self._message_queues.clear()
        self._request_session_ids.clear()
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception as e:
            logger.warning("%s 关闭 WebSocket 时异常: %s", _LOG_LABEL, e)
        finally:
            self._ws = None
            self._uri = None
        logger.info("%s 已断开", _LOG_LABEL)

    async def _message_receiver_loop(self) -> None:
        disconnect_payload: dict[str, Any] | None = None
        cleanup_connection_state = False
        try:
            while self._running and self._ws is not None:
                try:
                    raw = await self._ws.recv()
                    try:
                        data = self._decode_inbound(raw)
                    except ValueError as decode_err:
                        frame_preview: Any
                        if isinstance(raw, (str, bytes)):
                            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                            frame_preview = text[:160]
                        elif isinstance(raw, dict):
                            frame_preview = "dict"
                        else:
                            frame_preview = type(raw).__name__
                        logger.warning(
                            "%s 入站帧解包失败: %s frame_preview=%s",
                            _LOG_LABEL,
                            decode_err,
                            frame_preview,
                        )
                        continue
                    request_id = _wire_request_id_key(data.get("request_id"))
                    session_id = (
                        self._request_session_ids.get(request_id)
                        or str(data.get("session_id") or "").strip()
                        or "n/a"
                    )
                    _log_oa_inbound_frame(
                        data,
                        session_id=session_id,
                        sandbox_id=self._sandbox_id,
                    )
                    meta = data.get("metadata")
                    if isinstance(meta, dict) and meta.get(E2A_WIRE_SERVER_PUSH_KEY):
                        if self._on_server_push is not None:
                            asyncio.create_task(self._on_server_push(data))
                        else:
                            logger.warning(
                                "%s 收到 server_push 但未注册 handler，已丢弃: request_id=%s",
                                _LOG_LABEL,
                                data.get("request_id"),
                            )
                        continue
                    if self._dispatch_link_heartbeat(data):
                        continue
                    async with self._queue_lock:
                        if request_id in self._cancelled_request_ids:
                            logger.debug(
                                "%s 收到已取消请求的残余消息，已丢弃: request_id=%s",
                                _LOG_LABEL,
                                request_id,
                            )
                            continue
                        if request_id and request_id in self._message_queues:
                            interface_info.mark(
                                request_id,
                                interface_info.TimingPoint.OA_FIRST_RESPONSE_RECEIVED,
                            )
                            await self._message_queues[request_id].put(data)
                        else:
                            logger.debug(
                                "%s 收到无目标队列的消息: request_id=%s",
                                _LOG_LABEL,
                                request_id,
                            )
                except asyncio.CancelledError:
                    break
                except ConnectionClosed as exc:
                    logger.warning(
                        "%s 与 OA 的 WebSocket 物理断链: sandbox_id=%s code=%s reason=%s",
                        _LOG_LABEL,
                        self._sandbox_id,
                        getattr(exc, "code", None),
                        getattr(exc, "reason", None),
                    )
                    disconnect_payload = {
                        "event": "openability.connection_lost",
                        "sandbox_id": self._sandbox_id,
                        "uri": self._uri,
                        "code": getattr(exc, "code", None),
                        "reason": getattr(exc, "reason", None),
                    }
                    cleanup_connection_state = True
                    break
                except Exception as e:
                    logger.exception("%s 与 OA 的消息接收循环异常退出: %s", _LOG_LABEL, e)
                    cleanup_connection_state = True
                    break
        finally:
            if cleanup_connection_state:
                self._running = False
                self._server_ready = False
                ws = self._ws
                self._ws = None
                self._uri = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception as close_err:
                        logger.warning(
                            "%s 接收循环退出后关闭 WebSocket 失败: %s",
                            _LOG_LABEL,
                            close_err,
                        )
            if disconnect_payload is not None and self._on_connection_lost is not None:
                asyncio.create_task(self._on_connection_lost(disconnect_payload))
            logger.info("%s 消息接收任务已停止", _LOG_LABEL)

    def _dispatch_link_heartbeat(self, data: dict[str, Any]) -> bool:
        from jiuwenclaw.e2a.link_heartbeat import is_link_heartbeat_wire

        if not is_link_heartbeat_wire(data):
            return False
        if self._on_link_heartbeat is not None:
            logger.info(
                "%s 收到 AgentServer link heartbeat: sandbox_id=%s request_id=%s",
                _LOG_LABEL,
                self._sandbox_id,
                data.get("request_id"),
            )
            asyncio.create_task(self._on_link_heartbeat(data))
        else:
            logger.info(
                "%s 收到 link heartbeat 但未注册 handler: request_id=%s",
                _LOG_LABEL,
                data.get("request_id"),
            )
        return True

    def _ensure_connected(self) -> None:
        if self._ws is None:
            raise RuntimeError("未连接 OpenAbility，请先调用 connect(uri)")

    async def _send_wire_payload(self, detail: dict[str, Any]) -> None:
        await self._ws.send(self._encode_outbound(detail))

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        self._ensure_connected()
        envelope.is_stream = False
        rid = _wire_request_id_key(envelope.request_id)
        session_id = str(envelope.session_id or "").strip() or "n/a"
        logger.info(
            "[E2A][oa][nostream][out] sandbox_id=%s session_id=%s request_id=%s method=%s",
            self._sandbox_id,
            session_id,
            rid,
            envelope.method,
        )
        if rid in self._message_queues:
            raise RuntimeError(
                f"OpenAbilityWebSocketClient: duplicate in-flight request_id={rid!r}"
            )
        queue: asyncio.Queue = asyncio.Queue()
        self._message_queues[rid] = queue
        self._request_session_ids[rid] = session_id
        try:
            async with self._lock:
                payload = _e2a_to_wire(envelope)
                logger.debug(
                    "%s 发送(非流式): sandbox_id=%s session_id=%s request_id=%s method=%s payload_bytes=%d",
                    _LOG_LABEL,
                    self._sandbox_id,
                    session_id,
                    rid,
                    envelope.method,
                    len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")),
                )
                await self._send_wire_payload(payload)
                interface_info.mark(
                    rid, interface_info.TimingPoint.OA_REQUEST_SENT
                )
            try:
                data = await asyncio.wait_for(
                    queue.get(), timeout=self._request_timeout_seconds
                )
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f"OpenAbility 非流式请求超时 (request_id={rid}, "
                    f"timeout={self._request_timeout_seconds}s)"
                ) from e
            logger.info(
                "[E2A][oa][nostream][in] sandbox_id=%s session_id=%s request_id=%s method=%s",
                self._sandbox_id,
                session_id,
                rid,
                envelope.method,
            )
            interface_info.mark(
                rid, interface_info.TimingPoint.OA_FINAL_RESPONSE_RECEIVED
            )
            return parse_agent_server_wire_unary(data)
        finally:
            await self._drain_and_remove_queue(rid)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        self._ensure_connected()
        envelope.is_stream = True
        rid = _wire_request_id_key(envelope.request_id)
        session_id = str(envelope.session_id or "").strip() or "n/a"
        logger.info(
            "[E2A][oa][stream][out] sandbox_id=%s session_id=%s request_id=%s method=%s",
            self._sandbox_id,
            session_id,
            rid,
            envelope.method,
        )
        if rid in self._message_queues:
            raise RuntimeError(
                f"OpenAbilityWebSocketClient: duplicate in-flight request_id={rid!r}"
            )
        queue: asyncio.Queue = asyncio.Queue()
        self._message_queues[rid] = queue
        self._request_session_ids[rid] = session_id
        try:
            async with self._lock:
                payload = _e2a_to_wire(envelope)
                logger.debug(
                    "%s 发送(流式): sandbox_id=%s session_id=%s request_id=%s method=%s payload_bytes=%d",
                    _LOG_LABEL,
                    self._sandbox_id,
                    session_id,
                    rid,
                    envelope.method,
                    len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")),
                )
                await self._send_wire_payload(payload)
                interface_info.mark(
                    rid, interface_info.TimingPoint.OA_REQUEST_SENT
                )
            async for data in _iter_reordered_stream_frames(
                queue,
                sandbox_id=self._sandbox_id,
                session_id=session_id,
                request_id=rid,
            ):
                chunk = parse_agent_server_wire_chunk(data)
                if chunk.is_complete:
                    interface_info.mark(
                        rid,
                        interface_info.TimingPoint.OA_FINAL_RESPONSE_RECEIVED,
                    )
                yield chunk
            logger.info(
                "[E2A][oa][stream][done] sandbox_id=%s session_id=%s request_id=%s method=%s",
                self._sandbox_id,
                session_id,
                rid,
                envelope.method,
            )
        except asyncio.CancelledError:
            logger.info("%s 流式接收被取消: request_id=%s", _LOG_LABEL, rid)
            raise
        finally:
            await self._drain_and_remove_queue(rid)

    async def _drain_and_remove_queue(self, rid: str) -> None:
        async with self._queue_lock:
            self._request_session_ids.pop(rid, None)
            queue = self._message_queues.get(rid)
            if queue is None:
                return
            self._cancelled_request_ids.add(rid)
            del self._message_queues[rid]
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            asyncio.create_task(self._delayed_cleanup_cancelled_request_id(rid))

    async def _delayed_cleanup_cancelled_request_id(self, rid: str) -> None:
        await asyncio.sleep(2.0)
        async with self._queue_lock:
            self._cancelled_request_ids.discard(rid)
