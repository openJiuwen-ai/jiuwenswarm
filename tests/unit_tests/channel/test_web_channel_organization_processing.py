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
@pytest.mark.parametrize("snapshot_key", ["expert_requests", "organization_requests"])
async def test_disconnect_reconciles_live_requests_instead_of_clearing_work(monkeypatch, live, snapshot_key):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter(), agent_client=object())
    channel._track_session_busy(status("request", True, "expert"))
    fetch = AsyncMock(return_value=(True, {snapshot_key: live, **({"expert_requests": []} if snapshot_key == "organization_requests" else {})}))
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


@pytest.mark.parametrize("source", ["org_expert_background", "org_summary_background", "org_root_background", "org_root_delivery"])
def test_background_round_cannot_clear_another_round_or_owner(source):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("foreground", True))
    for round_id in ("a", "b"):
        event = status(round_id, True, "expert")
        event.payload["source"] = source
        channel._track_session_busy(event)
    ended = status("a", False, "expert")
    ended.payload["source"] = source
    channel._track_session_busy(ended)
    channel._track_session_busy(status("foreground", False))
    assert channel.is_session_busy("session")
    ended.payload["request_id"] = "b"
    channel._track_session_busy(ended)
    assert not channel.is_session_busy("session")


def test_missing_round_identity_does_not_create_an_unmatched_busy_entry():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    event = status("frame-id", True, "expert")
    event.payload.pop("request_id")
    channel._track_session_busy(event)
    assert not channel._expert_processing_requests


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


@pytest.mark.parametrize("event,role,terminal", [
    ("org.turn_completed", "leader", True), ("chat.error", "leader", True),
    ("chat.error", None, True), ("chat.error", "teammate", True),
    ("chat.error", "member", True), ("chat.error", "team_leader", True),
])
@pytest.mark.parametrize("source", ["org_expert_direct", "org_expert_background", "org_summary_background"])
def test_organization_terminal_fallback_preserves_other_rounds(event, role, terminal, source):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("first", True, "expert"))
    channel._track_session_busy(status("second", True, "expert"))
    ended = status("first", False, "expert")
    ended.payload["source"] = source
    ended.event_type = None if event == "org.turn_completed" else EventType.CHAT_ERROR
    ended.payload.update({"event_type": event, "role": role})
    ended.payload.pop("is_processing")
    channel._track_session_busy(ended)
    requests = channel._expert_processing_requests["session"]
    assert (("expert", "first") in requests) is not terminal
    assert ("expert", "second") in requests
    channel._track_session_busy(status("owner", True))
    ended.payload["request_id"] = "second"
    channel._track_session_busy(ended)
    assert "session" not in channel._expert_processing_requests
    assert channel.is_session_busy("session") is True
    channel._track_session_busy(status("owner", False))
    assert channel.is_session_busy("session") is False


@pytest.mark.parametrize("success", [False, True])
def test_owner_stop_only_releases_git_busy_guard_after_success(success):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("owner", True))
    response = status("stop", False)
    response.event_type = EventType.CHAT_INTERRUPT_RESULT
    response.payload.update({"source": "org_stop", "success": success})
    channel._track_session_busy(response)
    assert channel.is_session_busy("session") is not success


@pytest.mark.parametrize("envelope_session", [None, "", "other"])
@pytest.mark.parametrize("success", [False, True])
def test_organization_stop_uses_payload_session_and_preserves_uncancelled_requests(envelope_session, success):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("owner", True))
    channel._track_session_busy(status("first", True, "expert"))
    channel._track_session_busy(status("second", True, "expert"))
    response = status("stop", False)
    response.session_id = envelope_session
    response.event_type = EventType.CHAT_INTERRUPT_RESULT
    response.payload.update({
        "session_id": "session", "source": "org_stop", "success": success,
        "cancelled_requests": [{"team_id": "expert", "request_id": "first"}],
    })
    channel._track_session_busy(response)
    assert channel._session_busy["session"] is not success
    requests = channel._expert_processing_requests["session"]
    assert (("expert", "first") in requests) is not success
    assert ("expert", "second") in requests
    assert channel.is_session_busy("session") is True
    assert envelope_session not in channel._session_busy
    if success:
        response.payload["cancelled_requests"] = [{"team_id": "expert", "request_id": "second"}]
        channel._track_session_busy(response)
        assert channel.is_session_busy("session") is False
        assert "session" not in channel._expert_processing_requests


def test_organization_stop_without_session_does_not_create_busy_entry():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    response = status("stop", False)
    response.session_id = None
    response.payload.update({"source": "org_stop", "success": True})
    channel._track_session_busy(response)
    assert not channel._session_busy
    assert not channel._expert_processing_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("items,removed", [
    (None, False), ("invalid", False), ({}, False), ([None, "invalid", []], False),
    ([{"team_id": [], "request_id": "round"}], False),
    ([None, {"team_id": "expert", "request_id": "round"}], True),
])
async def test_malformed_stop_request_list_does_not_prevent_result_delivery(monkeypatch, items, removed):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._track_session_busy(status("round", True, "expert"))
    broadcast = AsyncMock()
    monkeypatch.setattr(channel, "_broadcast_to", broadcast)
    response = status("stop", False)
    response.event_type = EventType.CHAT_INTERRUPT_RESULT
    response.metadata = {"ws_id": "socket"}
    response.payload.update({
        "event_type": "chat.interrupt_result", "source": "org_stop", "success": True,
        "cancelled_requests": items,
    })
    channel._ws_by_id["socket"] = object()
    await channel.send(response)
    assert any(call.args[0].get("event") == "chat.interrupt_result" for call in broadcast.await_args_list)
    assert channel.is_session_busy("session") is not removed
