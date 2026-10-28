# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ResponseSink``：业务层的统一出口，如何发送消息收敛到传输层。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.server.ws_send import enforce_send_budget, send_wire_payload

logger = logging.getLogger(__name__)

#: ``SSESink`` 队列结束哨兵。
STREAM_DONE = object()

#: ``SSESink.finish`` 在队列满时等待消费者腾位的上限（秒）。
#: 超时即判定消费者已断开、放弃投递哨兵 —— 见 :meth:`SSESink.finish`。
FINISH_TIMEOUT = 5.0

#: 团队/对话增量。连接上未读字节过多时，这些帧先让路，协议层 pong 才能写进去。
_DELTA_EVENT_TYPES = frozenset({"chat.delta", "chat.reasoning"})
#: 未读发送缓冲超过该值就暂停增量帧。32KiB 大约是 websockets 默认 write_limit。
_WS_BACKLOG_BYTES = 32 * 1024
_WS_BACKLOG_POLL_S = 0.05
_WS_BACKLOG_MAX_POLLS = 40


@runtime_checkable
class ResponseSink(Protocol):
    """业务层出口协议。实现方负责编码与实际发送。"""

    async def send_unary(self, resp: AgentResponse, *, response_id: str | None = None) -> bool:
        """发送非流式响应。返回是否原样送出。"""
        ...

    async def send_chunk(
        self,
        chunk: AgentResponseChunk,
        *,
        sequence: int,
        response_id: str | None = None,
    ) -> bool:
        """发送一个流式分片。返回是否原样送出。"""
        ...

    async def send_error(
        self, request_id: str, message: str, *, code: str = "INTERNAL_ERROR", channel_id: str = ""
    ) -> bool:
        """发送错误响应。"""
        ...

    async def send_wire(self, wire: dict[str, Any]) -> bool:
        """逃生通道：已自行构造好 wire 帧的少数场景。

        （``encode_json_parse_error_wire`` / ``build_server_push_wire`` 等）
        """
        ...


def _chunk_event_type(chunk: AgentResponseChunk) -> str:
    payload = chunk.payload
    if isinstance(payload, dict):
        return str(payload.get("event_type") or "")
    return ""


def _write_buffer_size(ws: Any) -> int | None:
    """连接上还没被对端读走的字节数；探测不到时不拦截发送。"""
    transport = getattr(ws, "transport", None)
    getter = getattr(transport, "get_write_buffer_size", None)
    if not callable(getter):
        return None
    try:
        return int(getter())
    except (TypeError, ValueError, OSError, NotImplementedError):
        # asyncio.WriteTransport 基类直接抛 NotImplementedError。云上适配
        # 往往只有这个方法、没有真正的写缓冲；探测失败就当没有积压。
        return None


async def _wait_for_delta_room(ws: Any) -> None:
    """缓冲偏满时先停住增量帧，把写出机会留给 pong。"""
    size = _write_buffer_size(ws)
    if size is None or size <= _WS_BACKLOG_BYTES:
        return
    for _ in range(_WS_BACKLOG_MAX_POLLS):
        await asyncio.sleep(_WS_BACKLOG_POLL_S)
        size = _write_buffer_size(ws)
        if size is None or size <= _WS_BACKLOG_BYTES:
            return


def _error_response(request_id: str, message: str, code: str, channel_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        channel_id=channel_id,
        ok=False,
        payload={"error": message, "code": code},
    )


class WSSink:
    """WebSocket 实现：编码后经 ``send_wire_payload`` 写 socket。

    ``send_lock`` 是**连接级**共享锁（同一 socket 上多请求复用），因此本 sink
    每请求构造、但持有连接的锁引用，保证写不交错。
    """

    __slots__ = ("_ws", "_send_lock")

    def __init__(self, ws: Any, send_lock: asyncio.Lock) -> None:
        self._ws = ws
        self._send_lock = send_lock

    async def send_unary(self, resp: AgentResponse, *, response_id: str | None = None) -> bool:
        wire = encode_agent_response_for_wire(
            resp, response_id=response_id or resp.request_id or ""
        )
        return await self.send_wire(wire)

    async def send_chunk(
        self, chunk: AgentResponseChunk, *, sequence: int, response_id: str | None = None
    ) -> bool:
        wire = encode_agent_chunk_for_wire(
            chunk, response_id=response_id or chunk.request_id or "", sequence=sequence
        )
        if _chunk_event_type(chunk) in _DELTA_EVENT_TYPES:
            await _wait_for_delta_room(self._ws)
        return await self.send_wire(wire)

    async def send_error(
        self, request_id: str, message: str, *, code: str = "INTERNAL_ERROR", channel_id: str = ""
    ) -> bool:
        return await self.send_unary(_error_response(request_id, message, code, channel_id))

    async def send_wire(self, wire: dict[str, Any]) -> bool:
        async with self._send_lock:
            return await send_wire_payload(self._ws, wire)


class UnaryHTTPSink:
    """HTTP 非流式实现：持有业务对象，把序列化次数降到下限。

    handler 返回后由路由层读取 :attr:`response`（或 :attr:`wire`，当 handler
    走了 ``send_wire`` 逃生通道时）渲染 HTTP 响应。

    .. note:: 口径已定：**HTTP 非流式仍受 6MB 帧预算约束**

       与 WS 保持一致的跨传输行为，优先于省掉一次序列化。为量帧大小必须
       序列化一次，因此本类做不到「全程不序列化」；它省掉的是
       ``CollectingSink`` 那条路上的 ``json.loads``：

       ===============================  =========================
       路径                              序列化次数
       ===============================  =========================
       今天 ``WSSink(CollectingSink)``   dumps + loads + dumps
       本类                              dumps + dumps
       ===============================  =========================

       三个出口方法（``send_unary`` / ``send_chunk`` / ``send_wire``）都做判定，
       返回值与 :class:`WSSink` 同义：``False`` = 已降级。依赖这个信号中止后续
       发送的流式 handler 在 HTTP 下才有同样行为。
       守护：``test_all_sinks_share_one_send_budget_semantics``。
    """

    __slots__ = ("response", "wire", "frames")

    def __init__(self) -> None:
        #: 最后一次 ``send_unary`` / ``send_error`` 的业务对象（首选数据源）。
        self.response: AgentResponse | None = None
        #: 最后一次 ``send_wire`` 的原始帧（仅逃生通道会用到）。
        self.wire: dict[str, Any] | None = None
        #: 全部产出，按顺序保留，供需要多帧的场景使用。
        self.frames: list[AgentResponse | AgentResponseChunk | dict[str, Any]] = []

    async def send_unary(self, resp: AgentResponse, *, response_id: str | None = None) -> bool:
        """记下业务对象，并施加与 WS 相同的发送预算。

        **原样发送时不序列化第二次**：``self.response`` 仍是业务对象，路由层
        直接从它渲染 HTTP 响应。超预算时改存降级后的错误帧，且 ``response``
        置空，避免路由层拿原始超大对象继续渲染。
        """
        wire = encode_agent_response_for_wire(
            resp, response_id=response_id or resp.request_id or ""
        )
        payload, sent_original = enforce_send_budget(wire)
        if sent_original:
            self.response = resp
            # 预算判定已经把 wire 算出来了，留下它 —— 路由层渲染响应正好要用，
            # 省掉「序列化→反序列化」那一次往返（见类 docstring 的次数对比表）。
            self.wire = wire
            self.frames.append(resp)
        else:
            degraded = json.loads(payload)
            self.response = None
            self.wire = degraded
            self.frames.append(degraded)
        return sent_original

    async def send_chunk(
        self, chunk: AgentResponseChunk, *, sequence: int, response_id: str | None = None
    ) -> bool:
        """非流式入口收到 chunk：保留但不覆盖 response，交由路由层决定如何合并。

        同样施加预算 —— 返回值必须与 ``WSSink`` 同义，否则依赖 ``False`` 中止
        后续发送的流式 handler 在 HTTP 下会继续推。
        """
        wire = encode_agent_chunk_for_wire(
            chunk, response_id=response_id or chunk.request_id or "", sequence=sequence
        )
        payload, sent_original = enforce_send_budget(wire)
        self.frames.append(chunk if sent_original else json.loads(payload))
        return sent_original

    async def send_error(
        self, request_id: str, message: str, *, code: str = "INTERNAL_ERROR", channel_id: str = ""
    ) -> bool:
        return await self.send_unary(_error_response(request_id, message, code, channel_id))

    @property
    def last_frame(self) -> dict[str, Any] | None:
        """最后一帧的 wire 形式 —— 与 ``CollectingSink.last_frame`` 同义，

        让路由层不必区分自己拿到的是哪一种非流式 sink。

        探针：``wire`` 为空却有 ``frames``，说明 handler **确实产出了内容**，
        但都是 ``send_chunk`` 那类本 sink 渲染不出来的帧 —— 路由层会照常返回
        ``HTTP 200 + {"ok": true, "data": null}``，调用方拿到"成功"却什么都没有。

        这正是 ``/e2a`` 带 ``is_stream: true`` 时那个 bug 的 signature：它静默了
        很久，因为「产出了内容、渲染出来是空」在任何日志里都不留痕。该路径已修
        （改走 SSE），这条 warning 是留给**下一个**把流式 handler 接到非流式入口
        的人 —— 让同类问题第一次发生就可见，而不是等到有人来对账。
        """
        if self.wire is None and self.frames:
            logger.warning(
                "[UnaryHTTPSink] handler 产出了 %d 帧，但没有可渲染的响应帧 —— "
                "非流式入口接到了只发 chunk 的 handler，响应将退化为 data=null。"
                "该方法应走流式入口（SSE）",
                len(self.frames),
            )
        return self.wire

    async def send_wire(self, wire: dict[str, Any]) -> bool:
        """记下帧并施加与 WS 相同的发送预算。

        返回值必须和 ``WSSink`` 同义（``False`` = 已降级），否则依赖这个信号
        中止后续发送的流式 handler 在 HTTP 下会继续推 —— 本模块 docstring 把这条
        列为「关键」。此前这里无条件 ``return True``，是个只等着被踩的陷阱
        （当时本类尚无生产使用点，所以一直没暴露）。

        代价：为判定大小需要序列化一次，本类「全程不序列化」的初衷因此打了折扣。
        取舍是**正确性优先** —— 真要免掉这次序列化，得先决定「HTTP 是否仍受
        WS 帧预算约束」，那是产品口径问题，不是实现细节。
        """
        payload, sent_original = enforce_send_budget(wire)
        recorded = wire if sent_original else json.loads(payload)
        self.wire = recorded
        self.frames.append(recorded)
        return sent_original


class SSESink:
    """SSE 流式实现：把业务对象编码成 wire 帧后入队，由 SSE 生成器消费。

    仍走 ``encode_*_for_wire``，保证 SSE ``data:`` 与 WebSocket 帧**内容一致**，
    便于两侧对照排障；相比一期适配层省掉了 dumps→loads 往返。

    ``maxsize`` 提供背压：消费慢时阻塞 handler 的 send，避免内存无界增长。
    """

    __slots__ = ("queue",)

    def __init__(self, maxsize: int = 256) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    async def send_unary(self, resp: AgentResponse, *, response_id: str | None = None) -> bool:
        wire = encode_agent_response_for_wire(
            resp, response_id=response_id or resp.request_id or ""
        )
        return await self.send_wire(wire)

    async def send_chunk(
        self, chunk: AgentResponseChunk, *, sequence: int, response_id: str | None = None
    ) -> bool:
        wire = encode_agent_chunk_for_wire(
            chunk, response_id=response_id or chunk.request_id or "", sequence=sequence
        )
        return await self.send_wire(wire)

    async def send_error(
        self, request_id: str, message: str, *, code: str = "INTERNAL_ERROR", channel_id: str = ""
    ) -> bool:
        return await self.send_unary(_error_response(request_id, message, code, channel_id))

    async def send_wire(self, wire: dict[str, Any]) -> bool:
        """入队并施加与 WS 相同的发送预算。

        SSE 订阅者与 WS 订阅者对同一条推送必须给出相同结果：没有这道预算，
        一个 6MB+ 的 payload 会被原样吐给 SSE 客户端，而 WS 侧收到的是降级后的
        oversized 错误帧。两个传输对同一次推送分叉，正是要避免的情况。
        """
        payload, sent_original = enforce_send_budget(wire)
        await self.queue.put(wire if sent_original else json.loads(payload))
        return sent_original

    async def offer(self, item: Any) -> bool:
        """**有界**入队，供收尾路径使用；返回是否投递成功。

        与 :meth:`send_wire` 的区别：``send_wire`` 走的是**正常产出**路径，那里
        阻塞即背压、是正确行为（消费者在读，早晚腾出空位）。而收尾路径运行的时机
        恰恰是「消费者可能已经走了」：

        - 客户端中途断开 → ``sse_starlette`` 关闭生成器 → SSE 泵的 ``finally``
          取消 handler 任务 → 收尾代码在 ``_runner`` 的 ``finally`` 里执行；
        - 此时队列多半已被填满（``maxsize`` 背压）且**再无人消费**，
          阻塞式 ``put`` 会永远等下去，连带 ``await task`` 一起挂死、任务泄漏。

        因此分两步：先 ``put_nowait``（消费者正常时的常规路径，零等待）；
        队列满才退化为**有超时**的等待 —— 消费者只是慢，超时内会腾出空位；
        真的走了就放弃投递并返回 ``False``。

        不捕获 ``CancelledError``：任务被取消时它应当继续向外传播，
        让调用方看到「这是取消」而不是「正常结束」。
        """
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(self.queue.put(item), timeout=FINISH_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            return False

    async def finish(self) -> None:
        """handler 结束后放入哨兵，通知 SSE 生成器收尾。**保证不会永久阻塞。**"""
        if not await self.offer(STREAM_DONE):
            logger.warning(
                "[SSESink] 队列已满且 %.1fs 内无人消费，放弃投递结束哨兵"
                "（消费者已断开，无需收尾）",
                FINISH_TIMEOUT,
            )
