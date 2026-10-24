# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expert lifecycle events must not end the owner's or another expert's work."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def status(request_id, processing, team_id=""):
    return Message(
        id=request_id, type="event", channel_id="web", session_id="session",
        params={}, timestamp=0, ok=True, event_type=EventType.CHAT_PROCESSING_STATUS,
        payload={
            "event_type": "chat.processing_status", "is_processing": processing,
            "request_id": request_id,
            **({"team_id": team_id, "source": "org_expert_direct"} if team_id else {}),
        },
    )


@pytest.mark.parametrize("owner_running", [False, True])
def test_busy_aggregates_owner_and_all_expert_requests(owner_running):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("owner", owner_running))
    channel._track_session_busy(status("a", True, "expert"))
    channel._track_session_busy(status("b", True, "expert"))
    channel._track_session_busy(status("c", True, "other"))
    channel._track_session_busy(status("a", False, "expert"))
    channel._track_session_busy(status("old", False, "expert"))
    assert channel.is_session_busy("session") is True
    channel._track_session_busy(status("b", False, "expert"))
    assert channel.is_session_busy("session") is True
    channel._track_session_busy(status("c", False, "other"))
    assert channel.is_session_busy("session") is owner_running
    assert not channel._expert_processing_requests
    channel._track_session_busy(status("owner", False))
    assert channel.is_session_busy("session") is False


def test_missing_request_id_never_uses_message_frame_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    message = status("frame-1", True, "expert")
    message.payload.pop("request_id")
    channel._track_session_busy(message)
    assert not channel.is_session_busy("session")
    channel._track_session_busy(status("request", True, "expert"))
    message = status("frame-2", False, "expert")
    message.payload.pop("request_id")
    channel._track_session_busy(message)
    assert channel.is_session_busy("session")
    channel.clear_expert_processing("session")
    assert not channel.is_session_busy("session")


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [[], [{"team_id": "expert", "request_id": "request"}]])
async def test_disconnect_reconciles_live_requests_instead_of_clearing_work(monkeypatch, live):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter(), agent_client=object())
    channel._track_session_busy(status("request", True, "expert"))
    fetch = AsyncMock(return_value=(True, {"expert_requests": live}))
    monkeypatch.setattr("jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary", fetch)
    await channel.reconcile_expert_processing("session", "user")
    assert channel.is_session_busy("session") is bool(live)


@pytest.mark.asyncio
@pytest.mark.parametrize("ok,payload,busy", [
    (True, {"session_id": "session", "deleted": True}, False),
    (False, {"session_id": "session", "error": "not stopped"}, True),
    (True, {"session_id": "session", "operation_id": "pending"}, True),
])
async def test_session_close_cleans_records_only_after_completion(monkeypatch, ok, payload, busy):
    from jiuwenswarm.gateway.channel_manager.web.lifecycle_handlers import register_lifecycle_handlers

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("request", True, "expert"))
    monkeypatch.setattr(channel, "send_response", AsyncMock())
    monkeypatch.setattr(channel, "send_event", AsyncMock())
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.lifecycle_handlers.fetch_agent_unary",
        AsyncMock(return_value=(ok, payload)),
    )
    register_lifecycle_handlers(channel, lambda: object(), lambda: None)
    await channel._method_handlers["session.delete"](object(), "delete", {}, "session", "user")
    await asyncio.sleep(0)
    assert channel.is_session_busy("session") is busy


@pytest.mark.asyncio
async def test_expert_cancel_result_never_synthesizes_owner_status(monkeypatch):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    broadcast = AsyncMock()
    monkeypatch.setattr(channel, "_broadcast_to", broadcast)
    message = status("cancel", False, "expert")
    message.event_type = EventType.CHAT_INTERRUPT_RESULT
    message.payload.update({"event_type": "chat.interrupt_result", "success": True, "intent": "cancel"})
    channel._track_session_busy(status("owner", True))
    # Exercise the legacy broadcast path, where synthetic status used to leak.
    message.metadata = {"ws_id": "socket"}
    channel._ws_by_id["socket"] = object()
    await channel.send(message)
    frames = [call.args[0] for call in broadcast.await_args_list]
    assert any(frame.get("event") == "chat.interrupt_result" for frame in frames)
    assert not any(frame.get("event") == "chat.processing_status" for frame in frames)
    assert channel.is_session_busy("session") is True
