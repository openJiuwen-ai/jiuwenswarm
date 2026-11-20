# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把团队直播帧交给本进程里每一条已连上的网关推送流。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_subscribers: set[asyncio.Queue] = set()


def attach() -> asyncio.Queue:
    """登记一条网关 ``events/stream``。调用方断开时要 ``detach``。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(queue)
    return queue


def detach(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def publish(frame: dict[str, Any]) -> None:
    """投给当前所有推送流。没有听众就丢掉，不补放。"""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning("[TeamSession] gateway push queue full; dropping frame")


def reset() -> None:
    """测试清场。"""
    _subscribers.clear()
