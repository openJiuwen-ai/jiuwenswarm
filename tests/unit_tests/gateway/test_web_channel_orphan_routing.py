# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""RC-3 回归：孤儿请求事件不得改投 session 桶（跨请求串扰）。

场景：客户端断开重连后，旧连接在途流的迟到事件（metadata.ws_id 指向已
unregister 的物理连接）不能落入 session 桶投给新连接——否则新请求会消费到
旧请求的 chat.final。修复后这类事件直接丢弃，重连方通过 history 补齐。
"""

from __future__ import annotations

import time

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey


class _FakeWs:
    def __init__(self, ws_id: str, closed: bool = False) -> None:
        self._jiuwen_ws_id = ws_id
        self.closed = closed


def _make_channel() -> WebChannel:
    channel = object.__new__(WebChannel)
    channel._session_busy = {}
    channel._clients_by_key = {}
    channel._ws_by_id = {}
    channel._send_queues = {}
    return channel


def _chat_final_msg(ws_id: str | None, session_id: str = "sess-1") -> Message:
    metadata = {"ws_id": ws_id} if ws_id else {}
    return Message(
        id="req-old",
        type="event",
        channel_id="web",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "chat.final", "content": "旧请求的回复"},
        event_type=EventType.CHAT_FINAL,
        metadata=metadata,
    )


async def _capture_broadcast(channel: WebChannel):
    calls: list[tuple[dict, set]] = []

    async def _fake_broadcast(frame, clients):
        calls.append((frame, set(clients)))

    channel._broadcast_to = _fake_broadcast  # type: ignore[method-assign]
    return calls


async def test_orphan_stream_event_dropped_not_rerouted_to_new_ws() -> None:
    """旧连接已断开：迟到 chat.final 不投给同 session 桶里的新连接。"""
    channel = _make_channel()
    new_ws = _FakeWs("ws-new")
    rk = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[rk] = [new_ws]
    # 旧 ws（ws-old）已 unregister：不在 _ws_by_id（也不在桶里）。
    calls = await _capture_broadcast(channel)

    await channel.send(_chat_final_msg(ws_id="ws-old"))

    assert calls == [], "孤儿流事件必须丢弃，不得经 session 桶投给新连接"


async def test_orphan_res_dropped_not_rerouted_to_new_ws() -> None:
    """res 帧与发起请求一一对应：发起连接断开时同样丢弃。"""
    channel = _make_channel()
    new_ws = _FakeWs("ws-new")
    rk = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[rk] = [new_ws]
    calls = await _capture_broadcast(channel)

    msg = Message(
        id="req-old",
        type="res",
        channel_id="web",
        session_id="sess-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"data": 1},
        metadata={"ws_id": "ws-old"},
    )
    await channel.send(msg)

    assert calls == [], "孤儿 res 必须丢弃，不得改投 session 桶"


async def test_live_request_ws_still_receives() -> None:
    """发起连接仍在：事件仍精确投递到该物理连接（行为不变）。"""
    channel = _make_channel()
    live_ws = _FakeWs("ws-live")
    other_ws = _FakeWs("ws-other")
    rk = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[rk] = [other_ws]
    channel._ws_by_id["ws-live"] = live_ws
    calls = await _capture_broadcast(channel)

    await channel.send(_chat_final_msg(ws_id="ws-live"))

    assert len(calls) == 1
    frame, clients = calls[0]
    assert clients == {live_ws}
    assert frame["event"] == "chat.final"
    assert frame["payload"]["content"] == "旧请求的回复"


async def test_event_without_ws_id_keeps_session_fallback() -> None:
    """无 ws_id 的事件（会话级状态等）仍走 session 桶兜底（行为不变）。"""
    channel = _make_channel()
    new_ws = _FakeWs("ws-new")
    rk = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[rk] = [new_ws]
    calls = await _capture_broadcast(channel)

    await channel.send(_chat_final_msg(ws_id=None))

    assert len(calls) == 1
    _, clients = calls[0]
    assert clients == {new_ws}


async def test_team_routing_target_fallthrough_keeps_session_fallback() -> None:
    """team fan_out（routing_target）V2 未命中后的回退保留原语义，不受孤儿丢弃影响。"""
    from jiuwenswarm.gateway.routing.keys import WebDeliveryTarget
    from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

    channel = _make_channel()
    new_ws = _FakeWs("ws-new")
    # 新连接挂在 agent 模式的桶里（重连握手注册），而 team 订阅者的 rk（team
    # 模式）已随死连接摘除——V2 两级查找均未命中，落入 legacy session 兜底。
    rk_legacy = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[rk_legacy] = [new_ws]
    calls = await _capture_broadcast(channel)

    rk_team = RoutingKey(
        user_id="u1", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="team", id="default"), session_id="sess-1",
    )
    routing_target = RoutingTarget(
        intent="godview",
        routing_keys=[rk_team],
        delivery=WebDeliveryTarget(ws_id="ws-old"),
    )
    await channel.send(_chat_final_msg(ws_id="ws-old"), routing_target=routing_target)

    assert len(calls) == 1, "team 回退语义不变：仍投 session 桶"
    _, clients = calls[0]
    assert clients == {new_ws}


@pytest.mark.parametrize(
    "event_name,expected_method",
    [("chat.final", "info"), ("chat.error", "info"), ("chat.delta", "debug")],
)
def test_orphan_drop_log_level(event_name: str, expected_method: str) -> None:
    """终态事件（final/error）INFO 便于现场定位，其余 DEBUG 防刷屏。"""
    from unittest.mock import patch

    from jiuwenswarm.gateway.channel_manager.web import web_connect

    event_type = EventType(event_name)
    msg = Message(
        id="req-old",
        type="event",
        channel_id="web",
        session_id="sess-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_name, "content": "x"},
        event_type=event_type,
        metadata={"ws_id": "ws-old"},
    )
    with patch.object(web_connect, "logger") as mock_logger:
        web_connect.WebChannel._log_orphan_request_drop(msg, "ws-old")
    getattr(mock_logger, expected_method).assert_called_once()
    for other in ("info", "debug"):
        if other != expected_method:
            getattr(mock_logger, other).assert_not_called()
