# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""issue #1548 修复单测：网关出站消息顺序保证。

覆盖：
1. publish_robot_messages 入队原子性（无 await → 入队序 = 生产序，含 out_seq）
2. SessionSender：同 session 严格按入队顺序发送（含 outbound_apply 慢操作）
3. SessionSender：跨 session 并发发送（慢 session 不阻塞快 session）
4. SessionSender：发送失败退避重试，恢复后按序补发
5. server_push 单 FIFO worker：按到达顺序处理
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import BaseChannel
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager, SessionSender
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.gateway.routing.keys import ChannelKey


class RecordingChannel(BaseChannel):
    """记录到达顺序的通道桩；可注入失败次数模拟瞬时故障。"""

    def __init__(self, fail_first: int = 0, fail_delay: float = 0.0):
        super().__init__(config=None, router=None)
        self.received: list[str] = []
        self._fail_first = fail_first
        self._fail_delay = fail_delay

    @property
    def channel_id(self) -> str:
        return "rec"

    def on_message(self, callback) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: Message) -> None:
        if self._fail_delay:
            await asyncio.sleep(self._fail_delay)
        if self._fail_first > 0:
            self._fail_first -= 1
            raise ConnectionError("simulated transient failure")
        self.received.append(str((msg.payload or {}).get("content", "")))


class _NoopAgentClient:
    def set_server_push_handler(self, handler) -> None:
        self.push_handler = handler


def make_handler() -> MessageHandler:
    MessageHandler._instance = None
    return MessageHandler(_NoopAgentClient())


def make_message(mid: str, content: str, session: str, channel: str = "rec") -> Message:
    return Message(
        id=mid,
        type="event",
        channel_id=channel,
        session_id=session,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "chat.final", "content": content},
        metadata={},
    )


def start_sender(sender: SessionSender) -> None:
    """启动 SessionSender worker（测试内不经过 ChannelManager 时手动置 task）."""
    sender.task = asyncio.create_task(sender.run())


async def stop_sender(*senders: SessionSender) -> None:
    """取消并回收 SessionSender worker."""
    for sender in senders:
        if sender.task is None:
            continue
        sender.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender.task


def make_sender(channel: RecordingChannel, key: tuple[ChannelKey, str], outbound_apply=None) -> SessionSender:
    """构造直连固定 channel 实例的 SessionSender（不经 ChannelManager 的动态查找）."""
    return SessionSender(lambda: channel, outbound_apply, key)


# ---------------------------------------------------------------------------
# 1. publish 原子入队 + out_seq
# ---------------------------------------------------------------------------


async def test_publish_assigns_monotonic_out_seq_per_session() -> None:
    handler = make_handler()
    m1 = make_message("a1", "A", "s1")
    m2 = make_message("a2", "B", "s1")
    m3 = make_message("a3", "C", "s2")  # 不同 session 独立计数
    await handler.publish_robot_messages(m1)
    await handler.publish_robot_messages(m2)
    await handler.publish_robot_messages(m3)
    assert m1.metadata["out_seq"] == 1
    assert m2.metadata["out_seq"] == 2
    assert m3.metadata["out_seq"] == 1  # s2 独立


async def test_publish_enqueue_order_equals_production_order_under_concurrent_producers() -> None:
    """并发生产者（模拟 server_push task / process_stream task）并发 publish：
    入队顺序必须与调用顺序一致（先开始 await 的先入队）。"""
    handler = make_handler()
    order: list[str] = []

    async def producer(name: str, delay_before: float):
        await asyncio.sleep(delay_before)
        await handler.publish_robot_messages(make_message(name, name, "s1"))
        order.append(name)

    # A 先调用 publish（无延迟），B/C 在 A 的入队过程中（毫秒级）到达
    await asyncio.gather(
        producer("A", 0.0),
        producer("B", 0.002),
        producer("C", 0.004),
    )
    queued = []
    while not handler._robot_messages.empty():
        queued.append(handler._robot_messages.get_nowait().id)
    # 入队顺序 = 调用顺序（修复前若有入队前 await，后到者可插队）
    assert queued == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# 2/3/4. SessionSender 行为
# ---------------------------------------------------------------------------


async def test_session_sender_preserves_order_with_slow_outbound_apply() -> None:
    """同 session：outbound_apply 慢（模拟 LLM 分类）不影响发送顺序。"""
    channel = RecordingChannel()

    async def slow_apply(msg: Message) -> None:
        content = str((msg.payload or {}).get("content", ""))
        await asyncio.sleep(0.2 if content == "A" else 0.01)

    sender = make_sender(channel, (ChannelKey("rec", "default"), "s1"), slow_apply)
    start_sender(sender)
    for name in ("A", "B", "C"):
        sender.submit(make_message(name, name, "s1"))
    await asyncio.sleep(0.6)
    await stop_sender(sender)
    assert channel.received == ["A", "B", "C"]  # 修复前 B/C 会越过慢的 A


async def test_session_senders_run_concurrently_across_sessions() -> None:
    """跨 session：慢 session（每条 0.1s）不阻塞快 session。"""
    slow = RecordingChannel(fail_delay=0.1)
    fast = RecordingChannel()
    slow_sender = make_sender(slow, (ChannelKey("rec-slow", "default"), "slow"))
    fast_sender = make_sender(fast, (ChannelKey("rec-fast", "default"), "fast"))
    start_sender(slow_sender)
    start_sender(fast_sender)
    for i in range(3):
        slow_sender.submit(make_message(f"s{i}", f"s{i}", "slow", channel="rec-slow"))
        fast_sender.submit(make_message(f"f{i}", f"f{i}", "fast", channel="rec-fast"))
    # 快 session 3 条 0.1s 内发完；慢 session 需要 ~0.3s
    await asyncio.sleep(0.15)
    try:
        assert fast.received == ["f0", "f1", "f2"]  # 未被慢 session 阻塞
    finally:
        await stop_sender(slow_sender, fast_sender)


async def test_session_sender_retries_then_delivers_in_order(monkeypatch) -> None:
    """发送失败 → 指数退避重试 → 恢复后按序补发。"""
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.channel_manager._SEND_BACKOFF_BASE", 0.01)
    # 失败前 2 次（第 1 条消息上），第 3 次成功
    channel = RecordingChannel(fail_first=2)
    sender = make_sender(channel, (ChannelKey("rec", "default"), "s1"))
    start_sender(sender)
    sender.submit(make_message("m1", "m1", "s1"))
    sender.submit(make_message("m2", "m2", "s1"))
    await asyncio.sleep(0.3)
    await stop_sender(sender)
    assert channel.received == ["m1", "m2"]  # m1 重试成功后仍先于 m2


async def test_channel_manager_dispatch_creates_session_senders() -> None:
    """路由循环按 (channel, session) 建 SessionSender 并送达。"""
    handler = make_handler()
    cm = ChannelManager(handler)
    ch_s1 = RecordingChannel()
    ch_s2 = RecordingChannel()
    cm._channels[ChannelKey("rec1", "default")] = ch_s1
    cm._channels[ChannelKey("rec2", "default")] = ch_s2
    await cm.start_dispatch()
    try:
        await handler.publish_robot_messages(make_message("a", "a", "s1", channel="rec1"))
        await handler.publish_robot_messages(make_message("b", "b", "s1", channel="rec1"))
        await handler.publish_robot_messages(make_message("c", "c", "s2", channel="rec2"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (len(ch_s1.received) < 2 or len(ch_s2.received) < 1):
            await asyncio.sleep(0.01)
        assert ch_s1.received == ["a", "b"]
        assert ch_s2.received == ["c"]
    finally:
        await cm.stop_dispatch()


# ---------------------------------------------------------------------------
# 5. server_push FIFO worker
# ---------------------------------------------------------------------------


async def test_server_push_processed_in_arrival_order() -> None:
    """server_push 按到达顺序处理（修复前 per-message create_task 会乱序）。"""
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient

    client = WebSocketAgentServerClient()
    processed: list[str] = []

    async def handler(data: dict[str, Any]) -> None:
        name = str(data.get("name", ""))
        await asyncio.sleep(0.2 if name == "A" else 0.01)  # A 慢，B/C 快
        processed.append(name)

    client.set_server_push_handler(handler)
    # 模拟接收循环：按到达顺序入队
    for name in ("A", "B", "C"):
        client._server_push_queue.put_nowait({"name": name})
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(processed) < 3:
        await asyncio.sleep(0.01)
    assert processed == ["A", "B", "C"]  # FIFO：慢的 A 也不被越过
    await client.disconnect()
