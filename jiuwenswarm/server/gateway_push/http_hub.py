# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP ``events/stream`` 上的 server_push 分发。

企业版 Gateway 用 ``GATEWAY_AGENT_CLIENT_MODE=runtime`` 时不再挂 WebSocket，
改走 ``GET /api/v1/events/stream`` 收推送。``send_push`` 把编码后的 wire
放进这里，订阅中的 SSE 连接再转给 Gateway；没有订阅者时返回 0，由调用方
决定是否算投递失败。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# 单个 Gateway 订阅队列：cron ack 等不能无限堆积；满了丢最旧一帧再写入。
_SUBSCRIBER_QUEUE_SIZE = 256


class HttpPushHub:
    """进程内 fan-out：多个 Gateway 可同时订阅同一 AgentServer。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, wire: dict[str, Any]) -> int:
        """把一帧推给所有订阅者。返回成功入队的数量。"""
        with self._lock:
            subscribers = list(self._subscribers)
        delivered = 0
        for queue in subscribers:
            if self._enqueue(queue, wire):
                delivered += 1
        return delivered

    @staticmethod
    def _enqueue(
        queue: asyncio.Queue[dict[str, Any]],
        wire: dict[str, Any],
    ) -> bool:
        try:
            queue.put_nowait(wire)
            return True
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(wire)
                return True
            except asyncio.QueueFull:
                logger.warning(
                    "[HttpPushHub] subscriber queue full; drop server_push frame"
                )
                return False


_HUB = HttpPushHub()


def get_http_push_hub() -> HttpPushHub:
    return _HUB
