# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""断开重连后，原连接已不在的在途事件投给同 session 的新连接。

发起连接仍在时仍只投给该物理连接。A4P 改绑优先于 session 桶。
原连接已断开且没有改绑时，按 session_id 落入仍打开的连接。
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
    channel._a4p_rebound_ws = {}
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


async def test_orphan_stream_event_delivered_to_new_ws() -> None:
    """旧连接已断开：迟到 chat.final 投给同 session 桶里的新连接。"""
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

    assert len(calls) == 1
    frame, clients = calls[0]
    assert clients == {new_ws}
    assert frame["event"] == "chat.final"
    assert frame["payload"]["content"] == "旧请求的回复"


async def test_orphan_res_delivered_to_new_ws() -> None:
    """发起连接断开后，res 同样落到同 session 的新连接。"""
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

    assert len(calls) == 1
    frame, clients = calls[0]
    assert clients == {new_ws}
    assert frame["type"] == "res"
    assert frame["payload"] == {"data": 1}


@pytest.mark.parametrize("event_type", [EventType.CHAT_FINAL, EventType.CHAT_ERROR])
async def test_orphan_terminal_delivered_to_live_session_sockets(event_type: EventType) -> None:
    """原连接已断开的终态按 session_id 投给仍打开的连接，不投给已关闭连接和其他会话。"""
    channel = _make_channel()
    expected = _FakeWs("ws-new")
    other_user = _FakeWs("ws-other-user")
    closed = _FakeWs("ws-closed", closed=True)
    other_app = _FakeWs("ws-other-app")
    other_session = _FakeWs("ws-other-session")
    for app, session, sockets in [
        ("default", "sess-1", [expected, other_user, closed]),
        ("other", "sess-1", [other_app]),
        ("default", "sess-2", [other_session]),
    ]:
        route = RoutingKey(
            user_id="u1", channel_id="web", app_id=app,
            agent_ref=AgentRef(mode="agent", id="default"), session_id=session,
        )
        channel._clients_by_key[route] = sockets
    calls = await _capture_broadcast(channel)
    msg = _chat_final_msg("ws-old")
    msg.event_type = event_type

    await channel.send(msg)

    assert len(calls) == 1
    frame, clients = calls[0]
    assert frame["event"] == event_type.value
    assert clients == {expected, other_user, other_app}


async def test_orphan_delta_delivered_to_new_ws() -> None:
    """思考/正文增量在原连接断开后继续投给新连接，而不是按 token 触发历史重拉。"""
    channel = _make_channel()
    socket = _FakeWs("ws-new")
    route = RoutingKey(
        user_id="local", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[route] = [socket]
    calls = await _capture_broadcast(channel)
    msg = _chat_final_msg("ws-old")
    msg.event_type = EventType.CHAT_DELTA
    msg.metadata["user_id"] = None
    msg.app_id = "default"

    await channel.send(msg)

    assert len(calls) == 1
    frame, clients = calls[0]
    assert clients == {socket}
    assert frame["event"] == "chat.delta"
    assert "runtime.history_required" not in str(frame)


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


async def test_orphan_event_rebound_to_authorizing_ws() -> None:
    """刷新后经新连接完成授权：恢复 turn 的迟到终态改投到新连接（而非丢弃）。"""
    channel = _make_channel()
    rebound_ws = _FakeWs("ws-new")
    channel._ws_by_id["ws-new"] = rebound_ws
    channel.record_a4p_rebound("sess-1", "ws-new")
    calls = await _capture_broadcast(channel)

    await channel.send(_chat_final_msg(ws_id="ws-old"))

    assert len(calls) == 1
    frame, clients = calls[0]
    assert clients == {rebound_ws}
    assert frame["event"] == "chat.final"


async def test_orphan_rebound_cleared_after_terminal() -> None:
    """恢复 turn 终态送达后解除绑定。该连接不在 session 桶里时，后续事件不再改投。"""
    channel = _make_channel()
    rebound_ws = _FakeWs("ws-new")
    channel._ws_by_id["ws-new"] = rebound_ws
    channel.record_a4p_rebound("sess-1", "ws-new")
    calls = await _capture_broadcast(channel)

    await channel.send(_chat_final_msg(ws_id="ws-old"))
    assert len(calls) == 1

    delta = _chat_final_msg(ws_id="ws-old")
    delta.event_type = EventType.CHAT_DELTA
    await channel.send(delta)

    assert len(calls) == 1, "绑定应已解除，且该连接不在 session 桶里，后续事件不再改投"


def _payload_only_terminal_msg(event_name: str) -> Message:
    """终态消息只带 payload["event_type"]，event_type 字段为 None（_chunk_to_message 的合法形态）。"""
    return Message(
        id="req-old",
        type="event",
        channel_id="web",
        session_id="sess-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_name, "content": "旧请求的回复"},
        event_type=None,
        metadata={"ws_id": "ws-old"},
    )


async def test_orphan_terminal_payload_event_type_only_delivered_to_new_ws() -> None:
    """终态只带 payload event_type 时，原连接断开后仍投给同 session 的新连接。"""
    channel = _make_channel()
    expected = _FakeWs("ws-new")
    expected._web_connection_user_id = None
    route = RoutingKey(
        user_id="local", channel_id="web", app_id="default",
        agent_ref=AgentRef(mode="agent", id="default"), session_id="sess-1",
    )
    channel._clients_by_key[route] = [expected]
    calls = await _capture_broadcast(channel)

    msg = _payload_only_terminal_msg("chat.final")
    msg.metadata["user_id"] = None
    msg.app_id = "default"

    await channel.send(msg)

    assert len(calls) == 1
    frame, clients = calls[0]
    assert clients == {expected}
    assert frame["event"] == "chat.final"


async def test_orphan_rebound_cleared_on_payload_event_type_only_terminal() -> None:
    """终态只带 payload event_type 时也解除改绑，避免绑定残留改投后续迟到事件。"""
    channel = _make_channel()
    rebound_ws = _FakeWs("ws-new")
    channel._ws_by_id["ws-new"] = rebound_ws
    channel.record_a4p_rebound("sess-1", "ws-new")
    calls = await _capture_broadcast(channel)

    await channel.send(_payload_only_terminal_msg("chat.final"))
    assert len(calls) == 1

    delta = _chat_final_msg(ws_id="ws-old")
    delta.event_type = EventType.CHAT_DELTA
    await channel.send(delta)

    assert len(calls) == 1, "绑定应已解除，且该连接不在 session 桶里，后续事件不再改投"
