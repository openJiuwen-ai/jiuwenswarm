# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebSocket 回包口：业务函数把结果交给它，由它写回当前这条连接。"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.server.ws_send import send_wire_payload


class ResponseSink(Protocol):
    """一次请求的回包口。WebSocket 和以后的 HTTP 各自实现。"""

    async def send_response(self, response: AgentResponse) -> None:
        """送出一条完整响应。"""

    async def send_chunk(self, chunk: AgentResponseChunk, *, sequence: int) -> bool:
        """送出历史记录的一片。返回是否写成功。"""


class WSSink:
    """把一条完整响应编码后写回当前 WebSocket 连接。"""

    def __init__(self, ws: Any, send_lock: asyncio.Lock) -> None:
        self._ws = ws
        self._send_lock = send_lock

    async def send_response(self, response: AgentResponse) -> None:
        wire = encode_agent_response_for_wire(
            response,
            response_id=response.request_id,
        )
        async with self._send_lock:
            await send_wire_payload(self._ws, wire)

    async def send_chunk(self, chunk: AgentResponseChunk, *, sequence: int) -> bool:
        """把历史记录的一片编码后写回当前连接。

        历史可以分成很多片往外送。某一片太大写不进去时返回 False，
        调用方就停，不再继续送后面的片。
        """

        wire = encode_agent_chunk_for_wire(
            chunk,
            response_id=chunk.request_id,
            sequence=sequence,
        )
        async with self._send_lock:
            return await send_wire_payload(self._ws, wire)
