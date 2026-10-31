# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 回包口。一次性回包收成一段 JSON，流式回包收成一段段 SSE。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.enterprise.edition import is_enterprise

_PLAN_MODE_EXITED = "plan.mode_exited"
# 客户端断开后没人再取帧。队列有上限，满了就等，避免一轮长聊天把内存堆满。
_SSE_FRAME_QUEUE_LIMIT = 32
# 短于网关对 Agent HTTP 流的 600 秒读超时。只在企业版 SSE 上发送。
_SSE_KEEPALIVE_INTERVAL_SECONDS = 10.0
_SSE_KEEPALIVE_COMMENT = ": keepalive\n\n"
_SSE_KEEPALIVE_STOP_TIMEOUT_SECONDS = 1.0


def response_to_dict(response: AgentResponse) -> dict[str, Any]:
    """把一条完整响应收成网页能直接发出去的字典。"""
    return {
        "request_id": response.request_id,
        "channel_id": response.channel_id,
        "ok": response.ok,
        "payload": response.payload,
        "metadata": response.metadata,
    }


def chunk_to_dict(chunk: AgentResponseChunk, *, sequence: int) -> dict[str, Any]:
    """把流式的一片收成字典。``sequence`` 是这一片的序号。"""
    return {
        "request_id": chunk.request_id,
        "channel_id": chunk.channel_id,
        "payload": chunk.payload,
        "is_complete": chunk.is_complete,
        "metadata": chunk.metadata,
        "sequence": sequence,
    }


def _annotate_model_error(payload: dict[str, Any], session_id: str | None) -> None:
    if payload.get("code"):
        return
    try:
        from jiuwenswarm.common.auth.apig import classify_model_error
        from jiuwenswarm.common.auth.login_credentials import session_uses_login_model

        code = classify_model_error(
            str(payload.get("error") or ""),
            login_model=session_uses_login_model(session_id),
        )
    except Exception:  # noqa: BLE001 — 分类失败不该影响错误本身的上报
        return
    if code:
        payload["code"] = code
        payload["upstream"] = True


class _QuietKeepalive:
    """个人版 HTTP 不发空闲注释。这些方法留着，是为了流式处理函数能照常调用。"""

    @staticmethod
    def start() -> None:
        return None

    @staticmethod
    def notify_activity(*, terminal: bool = False) -> None:
        return None

    @staticmethod
    def signal_stop() -> None:
        return None

    @staticmethod
    async def stop() -> None:
        return None


class _EnterpriseSseKeepalive(_QuietKeepalive):
    """企业版队长流在 idle 后仍开着。空闲时写 SSE 注释，避免网关读超时。"""

    def __init__(self, sink: "SSESink") -> None:
        self._sink = sink
        self._stop = asyncio.Event()
        self._activity = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="sse-keepalive")

    def notify_activity(self, *, terminal: bool = False) -> None:
        self._activity.set()
        if terminal:
            self._stop.set()

    def signal_stop(self) -> None:
        self._stop.set()
        self._activity.set()

    async def stop(self) -> None:
        self.signal_stop()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_SSE_KEEPALIVE_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._activity.wait(),
                    timeout=_SSE_KEEPALIVE_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                if self._stop.is_set():
                    return
                self._sink.offer_keepalive()
                continue
            self._activity.clear()
            if self._stop.is_set():
                return


class UnaryHTTPSink:
    """收一条完整响应。路由把它变成 HTTP JSON。"""

    def __init__(self) -> None:
        self.body: dict[str, Any] | None = None
        self.status_code = 200

    async def send_response(self, response: AgentResponse) -> None:
        self.body = response_to_dict(response)

    async def send_chunk(self, chunk: AgentResponseChunk, *, sequence: int) -> bool:
        self.body = chunk_to_dict(chunk, sequence=sequence)
        return True

    @staticmethod
    def open_stream_keepalive(request: Any) -> _QuietKeepalive:
        return _QuietKeepalive()


class SSESink:
    """把每一片放进队列，路由按 SSE 往外推。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_SSE_FRAME_QUEUE_LIMIT)
        self._closed = False

    async def send_response(self, response: AgentResponse) -> None:
        await self._emit(response_to_dict(response))
        await self.close()

    async def send_chunk(self, chunk: AgentResponseChunk, *, sequence: int) -> bool:
        if self._closed:
            return False
        await self._emit(chunk_to_dict(chunk, sequence=sequence))
        if chunk.is_complete:
            await self.close()
        return True

    def open_stream_keepalive(self, request: Any) -> _QuietKeepalive:
        if not is_enterprise():
            return _QuietKeepalive()
        return _EnterpriseSseKeepalive(self)

    def offer_keepalive(self) -> None:
        """队列满时丢掉这一拍，不挡住正在写入的真实 chunk。"""
        if self._closed:
            return
        try:
            self._queue.put_nowait(_SSE_KEEPALIVE_COMMENT)
        except asyncio.QueueFull:
            return

    async def frames(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._put_dropping_oldest(None)

    def pending_frame_count(self) -> int:
        """还没被取走的帧数，含结束标记。"""
        return self._queue.qsize()

    async def _emit(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)
        await self._queue.put(f"data: {text}\n\n")

    def _put_dropping_oldest(self, item: str | None) -> None:
        """结束标记必须放进去。队列满时丢掉最旧的一帧，避免关流时卡死。"""
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue


async def deliver_runtime_event(
    sink: Any,
    event: Any,
    *,
    streaming: bool,
    sequence: int,
) -> bool:
    """没有 WebSocket 连接时，把运行时事件写进 HTTP 回包口。"""
    event_type = getattr(event, "event_type", "")
    if event_type == _PLAN_MODE_EXITED:
        payload = getattr(event, "payload", None)
        if streaming:
            await sink.send_chunk(
                AgentResponseChunk(
                    request_id=event.request_id,
                    channel_id=event.channel_id,
                    payload=payload if isinstance(payload, dict) else {},
                    is_complete=False,
                    metadata=getattr(event, "metadata", None) or {},
                ),
                sequence=sequence,
            )
        else:
            await sink.send_response(
                AgentResponse(
                    request_id=event.request_id,
                    channel_id=event.channel_id,
                    ok=True,
                    payload=payload if isinstance(payload, dict) else {},
                    metadata=getattr(event, "metadata", None),
                )
            )
        return True
    if not event.ok and event_type == "runtime.error":
        error_value = event.payload.get("error") if isinstance(event.payload, dict) else None
        await sink.send_response(
            AgentResponse(
                request_id=event.request_id,
                channel_id=event.channel_id,
                ok=False,
                payload={
                    "error": str(
                        error_value if error_value is not None else "Runtime execution failed"
                    )
                },
                metadata=getattr(event, "metadata", None),
            )
        )
        return True
    if streaming:
        payload = dict(event.payload) if isinstance(event.payload, dict) else None
        if not event.ok:
            payload = dict(payload or {})
            payload["event_type"] = "chat.error"
            payload.setdefault("error", "Runtime execution failed")
        if isinstance(payload, dict) and payload.get("event_type") == "chat.error":
            _annotate_model_error(payload, getattr(event, "session_id", None))
        return await sink.send_chunk(
            AgentResponseChunk(
                request_id=event.request_id,
                channel_id=event.channel_id,
                payload=payload,
                is_complete=bool(getattr(event, "is_complete", False)),
                metadata=getattr(event, "metadata", None) or {},
            ),
            sequence=sequence,
        )
    await sink.send_response(
        AgentResponse(
            request_id=event.request_id,
            channel_id=event.channel_id,
            ok=bool(event.ok),
            payload=event.payload if isinstance(event.payload, dict) else None,
            metadata=getattr(event, "metadata", None),
        )
    )
    return True
