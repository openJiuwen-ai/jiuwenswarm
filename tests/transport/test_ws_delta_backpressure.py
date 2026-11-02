# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""连接积压时，增量帧先停一下，把写出机会留给 pong。"""

from __future__ import annotations

import asyncio

from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.transports.sink import WSSink


class _Transport:
    def __init__(self, sizes: list[int]) -> None:
        self._sizes = list(sizes)

    def get_write_buffer_size(self) -> int:
        if len(self._sizes) == 1:
            return self._sizes[0]
        return self._sizes.pop(0)


class _Ws:
    def __init__(self, sizes: list[int]) -> None:
        self.transport = _Transport(sizes)
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def _chunk(event_type: str) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id="r1",
        channel_id="web",
        payload={"event_type": event_type, "content": "x"},
    )


async def test_reasoning_waits_until_backlog_drops():
    ws = _Ws([64 * 1024, 64 * 1024, 1024])
    sink = WSSink(ws, asyncio.Lock())
    started = asyncio.get_running_loop().time()
    await sink.send_chunk(_chunk("chat.reasoning"), sequence=1)
    elapsed = asyncio.get_running_loop().time() - started
    assert ws.sent
    assert elapsed >= 0.05


async def test_tool_result_does_not_wait_for_backlog():
    ws = _Ws([64 * 1024])
    sink = WSSink(ws, asyncio.Lock())
    started = asyncio.get_running_loop().time()
    await sink.send_chunk(_chunk("chat.tool_result"), sequence=1)
    elapsed = asyncio.get_running_loop().time() - started
    assert ws.sent
    assert elapsed < 0.05


class _UnimplementedTransport:
    def get_write_buffer_size(self) -> int:
        raise NotImplementedError


class _UnimplementedWs:
    def __init__(self) -> None:
        self.transport = _UnimplementedTransport()
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


async def test_unimplemented_write_buffer_still_sends_delta():
    ws = _UnimplementedWs()
    sink = WSSink(ws, asyncio.Lock())
    started = asyncio.get_running_loop().time()
    await sink.send_chunk(_chunk("chat.delta"), sequence=1)
    elapsed = asyncio.get_running_loop().time() - started
    assert ws.sent
    assert elapsed < 0.05
