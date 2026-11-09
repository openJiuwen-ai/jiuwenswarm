"""Host meeting playback works with the unmodified Gateway dependency."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_gateway.extensions.registry import ExtensionRegistry
from agent_gateway.message_handler.message_handler import MessageHandler
from agent_gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.common.voice.config import TtsConfig
from jiuwenswarm.common.voice_matching import load_tts_voice_catalog
from jiuwenswarm.extensions.meeting import gateway_playback
from jiuwenswarm.extensions.meeting.extension import MeetingApplicationPlugin
from jiuwenswarm.extensions.meeting.gateway_playback import GatewayPlaybackRoute
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


@pytest.fixture
def playback_boundary(monkeypatch):
    methods = {}
    channel = SimpleNamespace(
        register_method=lambda name, handler, **options: methods.__setitem__(name, handler),
        on_disconnect=lambda handler: None,
        send_event=AsyncMock(),
    )
    client = WebSocketAgentServerClient()
    handler = object.__new__(MessageHandler)
    handler.agent_client = client
    handler._stream_metadata = {}
    handler._stream_app_ids = {}
    handler._stream_sessions = {}
    handler._handle_evolution_chunk = AsyncMock(return_value=True)
    handler.publish_robot_messages = AsyncMock()
    handler._register_agent_server_push_handler()
    plugin = MeetingApplicationPlugin()
    registry = ExtensionRegistry(callback_framework=object(), config={}, logger=None)
    registry.register_application_plugin(plugin)
    registry.bind_application_plugins(channel, agent_client=client)
    monkeypatch.setattr(gateway_playback, "playback_ready", AsyncMock())
    catalog = load_tts_voice_catalog("cosyvoice")
    route = GatewayPlaybackRoute(
        channel, object(), lambda: client, "session-1", "meeting-1", "owner-1", None,
        TtsConfig(
            upstream_url="https://example.invalid", api_key="test",
            model=catalog.default_model, voice=catalog.default_voice,
        ),
        "startup-1",
    )
    payload = {
        "event_type": "meeting.playback", "event": "meeting.speech.ready", "route_id": route.id,
        "owner_id": route.owner_id, "session_id": route.session_id, "meeting_id": route.meeting_id,
        "request_id": "speech-1", "lease_token": "lease-1", "source_message_id": None,
        "speaker_id": "team-leader", "playback_id": "playback-1", "chunks": ["Hello"],
        "chunk_count": 1, "voice": "test",
    }
    return SimpleNamespace(
        client=client, handler=handler, plugin=plugin, route=route, payload=payload, methods=methods,
    )


async def activate(boundary):
    await boundary.methods["meeting.playback.ready"](
        boundary.route.ws, "ready-1", {}, boundary.route.session_id,
    )


def speech_wire(payload):
    return build_server_push_wire({
        "request_id": "playback-ready-rpc", "channel_id": "web",
        "session_id": "session-1", "payload": payload,
    })


async def deliver(boundary, payload):
    await activate(boundary)
    await boundary.client._on_server_push(speech_wire(payload))


@pytest.mark.asyncio
async def test_meeting_push_reaches_owner_with_browser_event(playback_boundary):
    boundary = playback_boundary

    await activate(boundary)
    boundary.client._schedule_server_push(speech_wire(boundary.payload))
    await next(iter(boundary.client._server_push_tails.values()))

    boundary.route.channel.send_event.assert_awaited_once_with(boundary.route.ws, "meeting.speech.ready", {
        key: value for key, value in boundary.payload.items()
        if key not in {"event_type", "event", "route_id", "owner_id"}
    })
    assert boundary.route.current_playback == "playback-1"
    assert boundary.route.records["playback-1"]["lease_token"] == "lease-1"
    boundary.handler.publish_robot_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_meeting_push_is_consumed_without_broadcast(playback_boundary):
    boundary = playback_boundary
    boundary.payload["route_id"] = "expired-route"

    await deliver(boundary, boundary.payload)

    boundary.route.channel.send_event.assert_not_awaited()
    boundary.handler.publish_robot_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_meeting_owner_is_rejected_without_broadcast(playback_boundary):
    boundary = playback_boundary
    boundary.payload["owner_id"] = "other-owner"

    with pytest.raises(ValueError, match="Foreign meeting playback push"):
        await deliver(boundary, boundary.payload)

    boundary.route.channel.send_event.assert_not_awaited()
    assert boundary.route.records == {}
    boundary.handler.publish_robot_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_plugin_still_delivers_cleanup_push(playback_boundary, monkeypatch):
    boundary = playback_boundary
    await activate(boundary)
    monkeypatch.setattr(boundary.plugin, "is_enabled", lambda: False)
    boundary.payload.update(event="meeting.playback.stop", stop_id="stop-1")
    boundary.route.stop_media = AsyncMock()

    await boundary.client._on_server_push(speech_wire(boundary.payload))
    await boundary.route.stops["stop-1"]

    assert boundary.route.revoked
    boundary.route.stop_media.assert_awaited_once()
    assert boundary.route.channel.send_event.await_args.args[1] == "meeting.playback.stop"
    boundary.handler.publish_robot_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_pushes_keep_the_original_gateway_handler(playback_boundary):
    boundary = playback_boundary

    await deliver(boundary, {"event_type": "chat.delta", "content": "ordinary output"})

    boundary.handler.publish_robot_messages.assert_awaited_once()
    message = boundary.handler.publish_robot_messages.await_args.args[0]
    assert message.payload["content"] == "ordinary output"
    boundary.route.channel.send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_playback_readiness_does_not_wrap_itself(playback_boundary):
    boundary = playback_boundary
    await activate(boundary)

    await deliver(boundary, {"event_type": "chat.delta", "content": "once"})

    boundary.handler.publish_robot_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_restores_original_push_handler(playback_boundary):
    boundary = playback_boundary
    await activate(boundary)

    await boundary.plugin.shutdown()

    assert boundary.client._on_server_push == boundary.handler._handle_agent_server_push


@pytest.mark.asyncio
async def test_shutdown_preserves_a_newer_push_handler(playback_boundary):
    boundary = playback_boundary
    await activate(boundary)
    replacement = AsyncMock()
    boundary.client.set_server_push_handler(replacement)

    await boundary.plugin.shutdown()

    assert boundary.client._on_server_push is replacement


@pytest.mark.asyncio
async def test_routed_gateway_push_callback_is_preserved(playback_boundary):
    boundary = playback_boundary
    original = AsyncMock()
    client = SimpleNamespace(_push_handler=original)
    client.set_server_push_handler = lambda callback: setattr(client, "_push_handler", callback)
    boundary.plugin._binding.agent_client = client
    await activate(boundary)

    await client._push_handler(speech_wire(boundary.payload))
    ordinary = speech_wire({"event_type": "chat.delta", "content": "routed"})
    await client._push_handler(ordinary)
    await boundary.plugin.shutdown()

    boundary.route.channel.send_event.assert_awaited_once()
    original.assert_awaited_once_with(ordinary)
    assert client._push_handler is original


@pytest.mark.asyncio
async def test_malformed_push_keeps_gateway_diagnostics(playback_boundary):
    boundary = playback_boundary
    original = AsyncMock()
    boundary.client.set_server_push_handler(original)
    await activate(boundary)
    wire = {"protocol_version": "invalid"}

    await boundary.client._on_server_push(wire)

    original.assert_awaited_once_with(wire)
    boundary.route.channel.send_event.assert_not_awaited()
