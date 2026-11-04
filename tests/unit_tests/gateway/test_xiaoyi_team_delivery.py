# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for Team messages delivered to a Xiaoyi human seat."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)
from jiuwenswarm.gateway.message_handler.join_exit_handlers import (
    _with_xiaoyi_team_task_hold,
)
from jiuwenswarm.gateway.routing.keys import XiaoyiDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

_AGENT_ID = "agent-user-1"
_PHYSICAL_SESSION_ID = "physical-session-1"
_LOGICAL_SESSION_ID = "logical-session-1"
_TASK_ID = "task-1"
_PUSH_ID = "push-1"


def _channel() -> XiaoyiChannel:
    channel = XiaoyiChannel(
        XiaoyiChannelConfig(
            channel_id="xiaoyi",
            agent_id=_AGENT_ID,
            api_id="api-1",
            push_id=_PUSH_ID,
            enable_streaming=True,
        ),
        RobotMessageRouter(),
    )
    channel._ws_connections = {"primary": object()}
    channel._send_text_response = AsyncMock()
    channel._send_ws_to_user = AsyncMock()
    channel._send_push_to_user = AsyncMock()
    channel._send_status_update_with_state = AsyncMock()
    return channel


def _active_route(channel: XiaoyiChannel, *, seen_at: float | None = None) -> None:
    channel._active_push_sessions[_AGENT_ID] = (
        _PHYSICAL_SESSION_ID,
        _TASK_ID,
        _PUSH_ID,
        time.time() if seen_at is None else seen_at,
    )


def _join_notice(*, hold: bool = True) -> Message:
    metadata = {
        "xiaoyi_session_id": _PHYSICAL_SESSION_ID,
        "xiaoyi_task_id": _TASK_ID,
        "xiaoyi_conversation_id": _LOGICAL_SESSION_ID,
        "xiaoyi_push_id": _PUSH_ID,
        "im_sender_user_id": _AGENT_ID,
    }
    if hold:
        metadata["xiaoyi_team_task_hold"] = True
    return Message(
        id="join-request",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "已加入 session", "is_complete": True},
        event_type=EventType.CHAT_FINAL,
        metadata=metadata,
    )


def _team_message() -> Message:
    return Message(
        id="team-message-1",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": "team.message",
            "event": {
                "type": "team.message.p2p",
                "from_member": "team_leader",
                "to_member": "player-1",
                "content": "#count-3 complete",
            },
        },
        event_type=EventType.TEAM_MESSAGE,
    )


def _routing_target() -> RoutingTarget:
    return RoutingTarget(
        intent="mention",
        member_names=("player-1",),
        delivery=XiaoyiDeliveryTarget(
            agent_id=_AGENT_ID,
            push_id=_PUSH_ID,
            xiaoyi_session_id=_PHYSICAL_SESSION_ID,
            conversation_id=_LOGICAL_SESSION_ID,
        ),
    )


def test_join_notice_hold_marker_is_xiaoyi_only() -> None:
    user_infos = {"id": "request-1", "meta_data": {"existing": "value"}}

    xiaoyi = _with_xiaoyi_team_task_hold(user_infos, "xiaoyi")
    feishu = _with_xiaoyi_team_task_hold(user_infos, "feishu")

    assert xiaoyi["meta_data"] == {"existing": "value", "xiaoyi_team_task_hold": True}
    assert user_infos["meta_data"] == {"existing": "value"}
    assert feishu is user_infos


@pytest.mark.asyncio
async def test_join_notice_keeps_active_route_open() -> None:
    channel = _channel()
    _active_route(channel)

    await channel.send(_join_notice())

    channel._send_text_response.assert_awaited_once()
    send_kwargs = channel._send_text_response.await_args.kwargs
    assert send_kwargs["last_chunk"] is True
    assert send_kwargs["is_final"] is False
    assert _AGENT_ID in channel._active_push_sessions
    assert (_PHYSICAL_SESSION_ID, _TASK_ID) in channel._team_task_holds


@pytest.mark.asyncio
async def test_held_join_routes_team_p2p_to_websocket() -> None:
    channel = _channel()
    _active_route(channel)
    await channel.send(_join_notice())

    await channel.send(_team_message(), routing_target=_routing_target())

    channel._send_ws_to_user.assert_awaited_once()
    channel._send_push_to_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_deferred_status_keeps_route_for_followup_team_output() -> None:
    channel = _channel()
    _active_route(channel)
    deferred = Message(
        id="deferred-1",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "chat.processing_status_deferred", "is_complete": True},
        event_type=None,
        metadata={
            "xiaoyi_session_id": _PHYSICAL_SESSION_ID,
            "xiaoyi_task_id": _TASK_ID,
            "xiaoyi_conversation_id": _LOGICAL_SESSION_ID,
            "im_sender_user_id": _AGENT_ID,
            "member_name": "human-counter",
        },
    )

    await channel.send(deferred)

    send_kwargs = channel._send_text_response.await_args.kwargs
    assert send_kwargs["is_final"] is False
    assert _AGENT_ID in channel._active_push_sessions
    assert (_PHYSICAL_SESSION_ID, _TASK_ID) in channel._team_task_holds

    await channel.send(_team_message(), routing_target=_routing_target())

    channel._send_ws_to_user.assert_awaited_once()
    channel._send_push_to_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_held_team_final_stays_open_until_round_terminal() -> None:
    channel = _channel()
    _active_route(channel)
    await channel.send(_join_notice())
    final_msg = Message(
        id="team-final-1",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "member result", "is_complete": True},
        event_type=EventType.CHAT_FINAL,
    )

    await XiaoyiChannel._send_ws_to_user(
        channel,
        _PHYSICAL_SESSION_ID,
        _TASK_ID,
        final_msg,
        "member result",
    )

    send_kwargs = channel._send_text_response.await_args.kwargs
    assert send_kwargs["last_chunk"] is True
    assert send_kwargs["is_final"] is False
    assert _AGENT_ID in channel._active_push_sessions
    assert (_PHYSICAL_SESSION_ID, _TASK_ID) in channel._team_task_holds


@pytest.mark.asyncio
async def test_held_team_error_text_stays_open_until_round_terminal() -> None:
    channel = _channel()
    _active_route(channel)
    await channel.send(_join_notice())
    channel._send_text_response.reset_mock()
    error = Message(
        id="team-error-content-1",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=False,
        payload={"error": "team failed", "is_complete": True},
        event_type=EventType.CHAT_ERROR,
    )

    await XiaoyiChannel._send_ws_to_user(
        channel,
        _PHYSICAL_SESSION_ID,
        _TASK_ID,
        error,
        "team failed",
    )

    send_kwargs = channel._send_text_response.await_args.kwargs
    assert send_kwargs["last_chunk"] is True
    assert send_kwargs["is_final"] is False
    assert _AGENT_ID in channel._active_push_sessions
    assert (_PHYSICAL_SESSION_ID, _TASK_ID) in channel._team_task_holds


@pytest.mark.asyncio
async def test_team_round_terminal_closes_held_join_task() -> None:
    channel = _channel()
    _active_route(channel)
    await channel.send(_join_notice())
    terminal = Message(
        id="team-terminal-1",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"is_processing": False, "is_complete": True},
        event_type=EventType.CHAT_PROCESSING_STATUS,
    )

    await channel.send(terminal, routing_target=_routing_target())

    channel._send_status_update_with_state.assert_awaited_once()
    assert channel._send_status_update_with_state.await_args.kwargs["is_final"] is True
    assert _AGENT_ID not in channel._active_push_sessions
    assert (_PHYSICAL_SESSION_ID, _TASK_ID) not in channel._team_task_holds


@pytest.mark.asyncio
async def test_terminal_status_without_hold_does_not_send_empty_final() -> None:
    channel = _channel()
    _active_route(channel)
    terminal = Message(
        id="team-terminal-no-hold",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"is_processing": False, "is_complete": True},
        event_type=EventType.CHAT_PROCESSING_STATUS,
    )

    await channel.send(terminal, routing_target=_routing_target())

    channel._send_text_response.assert_not_awaited()
    channel._send_ws_to_user.assert_not_awaited()
    channel._send_status_update_with_state.assert_not_awaited()
    channel._send_push_to_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_final_retains_existing_cleanup() -> None:
    channel = _channel()
    _active_route(channel)

    await channel.send(_join_notice(hold=False))

    send_kwargs = channel._send_text_response.await_args.kwargs
    assert send_kwargs["is_final"] is True
    assert _AGENT_ID not in channel._active_push_sessions
    assert channel._team_task_holds == set()


@pytest.mark.asyncio
async def test_exit_after_join_releases_held_task() -> None:
    channel = _channel()
    _active_route(channel)
    await channel.send(_join_notice())
    channel._send_text_response.reset_mock()

    await channel.send(_join_notice(hold=False))

    send_kwargs = channel._send_text_response.await_args.kwargs
    assert send_kwargs["is_final"] is True
    assert _AGENT_ID not in channel._active_push_sessions
    assert channel._team_task_holds == set()


@pytest.mark.asyncio
async def test_team_error_releases_held_task() -> None:
    channel = _channel()
    _active_route(channel)
    await channel.send(_join_notice())
    error = Message(
        id="team-error-1",
        type="event",
        channel_id="xiaoyi",
        session_id=_LOGICAL_SESSION_ID,
        params={},
        timestamp=time.time(),
        ok=False,
        payload={"error": "team failed", "is_complete": True},
        event_type=EventType.CHAT_ERROR,
    )

    await channel.send(error, routing_target=_routing_target())

    assert channel._send_status_update_with_state.await_args.kwargs["is_final"] is True
    assert _AGENT_ID not in channel._active_push_sessions
    assert channel._team_task_holds == set()


@pytest.mark.asyncio
async def test_offline_team_message_still_falls_back_to_push() -> None:
    channel = _channel()
    _active_route(channel, seen_at=time.time() - 120)

    await channel.send(_team_message(), routing_target=_routing_target())

    channel._send_ws_to_user.assert_not_awaited()
    channel._send_push_to_user.assert_awaited_once()
