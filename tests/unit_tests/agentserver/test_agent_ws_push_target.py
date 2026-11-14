"""send_push 定向网关（PUSH_TARGET_KEY）路由单元测试。

2026-11-14「立即执行」断链根因回归防护：desktop 会话的 cron 转发帧
channel_id=desktop，常规 channel-map 路由会落到 PC 前端 stdio 连接被
静默丢弃；携带 ``claw_push_target=gateway`` 时必须优先投递
``__gateway__`` 绑定（gateway 管道连接）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.gateway_push import (
    GATEWAY_CONN_NAME,
    PUSH_TARGET_GATEWAY,
    PUSH_TARGET_KEY,
)


class _FakeTransport:
    """鸭子类型传输（send_wire_payload 同时接受 send_text/send）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _make_server(
    bindings: dict[str, _FakeTransport],
) -> AgentWebSocketServer:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._channel_conns = {
        name: (transport, asyncio.Lock()) for name, transport in bindings.items()
    }
    server._current_ws = None
    server._current_send_lock = None
    return server


def _cron_push_payload(channel_id: str) -> dict:
    """构造与 cron_tools._send_split 等价的转发帧（response_kind 分支）。"""
    return {
        "request_id": "req-1",
        "channel_id": channel_id,
        "session_id": "sess-1",
        "response_kind": "cron",
        "is_final": False,
        "status": "in_progress",
        "body": {
            "action": "run_now",
            "status": "ok",
            "data": {"job_id": "j-1"},
            "message": "",
        },
    }


@pytest.mark.asyncio
async def test_gateway_target_prefers_gateway_binding() -> None:
    gateway_ws = _FakeTransport("gateway-pipe")
    pc_ws = _FakeTransport("pc-stdio")
    server = _make_server({GATEWAY_CONN_NAME: gateway_ws, "desktop": pc_ws})

    payload = _cron_push_payload("desktop")
    payload[PUSH_TARGET_KEY] = PUSH_TARGET_GATEWAY
    await server.send_push(payload)

    assert len(gateway_ws.sent) == 1
    assert pc_ws.sent == []
    # 定向键只用于传输层选路，不得透传进 wire 帧
    assert PUSH_TARGET_KEY not in gateway_ws.sent[0]
    assert gateway_ws.sent[0].get("channel") == "desktop"
    assert gateway_ws.sent[0].get("response_kind") == "cron"


@pytest.mark.asyncio
async def test_gateway_target_falls_back_to_channel_map_when_unbound() -> None:
    """__gateway__ 未登记（如旧形态无管道连接）时回退常规路由，不丢帧。"""
    pc_ws = _FakeTransport("pc-stdio")
    server = _make_server({"desktop": pc_ws})

    payload = _cron_push_payload("desktop")
    payload[PUSH_TARGET_KEY] = PUSH_TARGET_GATEWAY
    await server.send_push(payload)

    assert len(pc_ws.sent) == 1


@pytest.mark.asyncio
async def test_no_target_keeps_channel_map_routing() -> None:
    """未携带定向键的帧保持原 channel-map 语义（回归防护）。"""
    gateway_ws = _FakeTransport("gateway-pipe")
    pc_ws = _FakeTransport("pc-stdio")
    server = _make_server({GATEWAY_CONN_NAME: gateway_ws, "desktop": pc_ws})

    await server.send_push(_cron_push_payload("desktop"))

    assert len(pc_ws.sent) == 1
    assert gateway_ws.sent == []
