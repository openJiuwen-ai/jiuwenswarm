# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebChannel 对非字符串 params.mode / params.agent_id 的前置拒绝（Issue #4876）。

回归背景：mode 传数组/对象时会流入 AgentRef → RoutingKey，在 register_ws 的
dict key hash 处抛 ``TypeError: unhashable type``，异常被逐消息任务静默吞掉，
客户端收不到任何 res/event。修复后在分发前校验并直接回 BAD_REQUEST。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import AgentRef
from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from gateway.routing.keys import RoutingKey


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, data: str) -> None:
        self.sent_frames.append(json.loads(data))


def _req(*, req_id: str, method: str, params: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        },
        ensure_ascii=False,
    )


async def _make_registered_channel(
    ws: _FakeWebSocket,
) -> tuple[WebChannel, list[Any]]:
    """构建已走握手注册的 channel（出站队列可用），返回 (channel, seen_messages)."""
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    seen: list[Any] = []
    channel.on_message(lambda msg: seen.append(msg))
    await channel.register_ws(
        ws,
        RoutingKey(
            user_id="local-peer",
            channel_id="web",
            app_id="default",
            agent_ref=AgentRef(mode="agent", id="default"),
            session_id="sess-initial",
        ),
    )
    return channel, seen


async def _wait_for_frames(ws: _FakeWebSocket, count: int = 1) -> None:
    for _ in range(40):
        if len(ws.sent_frames) >= count:
            return
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mode", [["team"], {"team": True}, 123, True, None])
async def test_non_string_mode_rejected_with_bad_request(bad_mode):
    ws = _FakeWebSocket()
    channel, seen = await _make_registered_channel(ws)
    try:
        await channel._handle_raw_message(
            ws,
            _req(
                req_id="req-4876",
                method="chat.interrupt",
                params={
                    "session_id": "web_1a@c6e71385_1064cd28ee05",
                    "intent": "pause",
                    "project_id": "default",
                    "mode": bad_mode,
                    "team": True,
                },
            ),
            {},
        )
        await _wait_for_frames(ws)

        assert len(ws.sent_frames) == 1
        frame = ws.sent_frames[0]
        assert frame["type"] == "res"
        assert frame["id"] == "req-4876"
        assert frame["ok"] is False
        assert frame["code"] == "BAD_REQUEST"
        assert "params.mode must be a string" in frame["error"]
        # 请求在校验处被拒绝：未进入分发，也不更新 ws 路由注册
        assert seen == []
        assert id(ws) not in channel._ws_sessions
    finally:
        await channel.unregister_ws(ws)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_agent_id", [["default"], {"id": "x"}, 42])
async def test_non_string_agent_id_rejected_with_bad_request(bad_agent_id):
    ws = _FakeWebSocket()
    channel, seen = await _make_registered_channel(ws)
    try:
        await channel._handle_raw_message(
            ws,
            _req(
                req_id="req-agent-id",
                method="chat.send",
                params={"session_id": "sess-a", "content": "hi", "agent_id": bad_agent_id},
            ),
            {},
        )
        await _wait_for_frames(ws)

        assert len(ws.sent_frames) == 1
        frame = ws.sent_frames[0]
        assert frame["type"] == "res"
        assert frame["ok"] is False
        assert frame["code"] == "BAD_REQUEST"
        assert "params.agent_id must be a string" in frame["error"]
        assert seen == []
        assert id(ws) not in channel._ws_sessions
    finally:
        await channel.unregister_ws(ws)


@pytest.mark.asyncio
async def test_string_mode_still_dispatches_normally():
    ws = _FakeWebSocket()
    channel, seen = await _make_registered_channel(ws)
    try:
        await channel._handle_raw_message(
            ws,
            _req(
                req_id="req-ok",
                method="chat.send",
                params={"session_id": "sess-ok", "content": "hi", "mode": "team"},
            ),
            {},
        )
        await asyncio.sleep(0.02)

        # 消息正常分发；不存在 BAD_REQUEST 拒绝帧（后续可能有无 handler 的
        # "unknown method" res，与本回归无关）
        assert len(seen) == 1
        assert channel._ws_sessions.get(id(ws)) == {"sess-ok"}
        assert all(f.get("code") != "BAD_REQUEST" for f in ws.sent_frames)
    finally:
        await channel.unregister_ws(ws)
