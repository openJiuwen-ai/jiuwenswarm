"""Regression tests for the Gateway's browser meeting playback RPCs."""

from base64 import b64decode
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
import pytest_asyncio
from agent_gateway.routing.agent_client import WebSocketAgentServerClient

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.voice.config import TtsConfig
from jiuwenswarm.common.voice_matching import load_tts_voice_catalog
from jiuwenswarm.extensions.meeting import gateway_playback
from jiuwenswarm.extensions.meeting.gateway_binding import MeetingGatewayBinding
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


@pytest_asyncio.fixture
async def playback(monkeypatch):
    methods = {}
    channel = SimpleNamespace(
        channel_id="web",
        register_method=lambda name, handler, **options: methods.__setitem__(name, handler),
        on_disconnect=lambda handler: None,
        send_event=AsyncMock(),
        send_response=AsyncMock(),
    )
    catalog = load_tts_voice_catalog("cosyvoice")
    config = TtsConfig(catalog.default_url, "test-key", catalog.default_model, catalog.default_voice)
    monkeypatch.setattr(TtsConfig, "from_environment", lambda: config)
    fetch = AsyncMock(return_value=(True, {"accepted": True}))
    monkeypatch.setattr(gateway_playback, "fetch_agent_unary", fetch)
    synthesize = AsyncMock(return_value=SimpleNamespace(audio=b"test audio", audio_mime="audio/wav"))
    monkeypatch.setattr(gateway_playback.TtsSession, "start", AsyncMock())
    monkeypatch.setattr(gateway_playback.TtsSession, "synthesize", synthesize)

    client = WebSocketAgentServerClient()
    client.set_server_push_handler(AsyncMock())
    binding = MeetingGatewayBinding(channel, client)
    binding.bind()
    ws = SimpleNamespace(
        _meeting_owner_id="owner-1",
        _meeting_owned_sessions={("session-1", "startup-1"): None},
    )
    identity = {
        "session_id": "session-1", "meeting_id": "meeting-1", "request_id": "speech-1",
        "lease_token": "lease-1", "source_message_id": None, "speaker_id": "alice",
        "playback_id": "playback-1",
    }

    async def request(method: str, params: dict, *, connection=ws) -> dict:
        channel.send_response.reset_mock()
        await methods[method](connection, "browser-request", params, identity["session_id"])
        channel.send_response.assert_awaited_once()
        return channel.send_response.await_args.kwargs

    try:
        ready = await request("meeting.playback.ready", {"meeting_id": "meeting-1", "startup_id": "startup-1"})
        assert ready["ok"], ready
        registration = fetch.await_args.kwargs
        assert registration["req_method"] == ReqMethod.MEETING_PLAYBACK_READY
        chunks = ["First chunk.", "Second chunk."]
        await client._on_server_push(build_server_push_wire({
            "request_id": "ready-1", "channel_id": "web", "session_id": "session-1",
            "payload": {
                **identity, "event_type": "meeting.playback", "event": "meeting.speech.ready",
                "route_id": registration["params"]["_meeting_route_id"], "owner_id": "owner-1",
                "voice": config.voice, "model": config.model, "chunks": chunks, "chunk_count": len(chunks),
            },
        }))
        channel.send_event.assert_awaited_once()
        fetch.reset_mock()
        yield SimpleNamespace(
            request=request, identity=identity, fetch=fetch, synthesize=synthesize,
            chunks=chunks, config=config, revision=registration["params"]["_tts_revision"],
        )
    finally:
        await gateway_playback.forget_playback_routes(ws, "session-1")
        await binding.aclose()


@pytest.mark.asyncio
async def test_meeting_playback_completes_only_after_every_chunk_is_synthesized(playback):
    receipt = {**playback.identity, "outcome": "completed", "completed_chunks": 2}

    async def agent_reply(*, req_method, params, **kwargs):
        if req_method == ReqMethod.MEETING_SPEECH_GET:
            return True, {
                "text": playback.chunks[params["chunk_index"]], "voice": playback.config.voice,
                "model": playback.config.model, "config_revision": playback.revision,
            }
        assert req_method == ReqMethod.MEETING_PLAYBACK_RESULT
        return True, {"accepted": True}

    playback.fetch.side_effect = agent_reply
    for index, chunk in enumerate(playback.chunks):
        audio = await playback.request("meeting.speech.synthesize", {**playback.identity, "chunk_index": index})
        assert audio["ok"], audio
        assert audio["payload"]["chunk_index"] == index
        assert b64decode(audio["payload"]["audio_base64"]) == b"test audio"
        assert playback.synthesize.await_args == call(chunk, playback.config.voice)
        if index == 0:
            early = await playback.request("meeting.playback.result", receipt)
            assert early["ok"] is False
            assert early["code"] == "MEETING_PLAYBACK_RESULT_FAILED"

    result = await playback.request("meeting.playback.result", receipt)

    assert result == {"ok": True, "payload": {"accepted": True}}
    assert [entry.kwargs["req_method"] for entry in playback.fetch.await_args_list] == [
        ReqMethod.MEETING_SPEECH_GET,
        ReqMethod.MEETING_SPEECH_GET,
        ReqMethod.MEETING_SPEECH_GET,
        ReqMethod.MEETING_SPEECH_GET,
        ReqMethod.MEETING_PLAYBACK_RESULT,
    ]
    assert playback.fetch.await_args.kwargs["params"] == {**receipt, "_meeting_owner_id": "owner-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"lease_token": "stale-token"}, {"text": "injected text"}])
async def test_meeting_synthesis_rejects_stale_or_client_supplied_speech(playback, override):
    params = {**playback.identity, "chunk_index": 0, **override}

    result = await playback.request("meeting.speech.synthesize", params)

    assert result["ok"] is False
    assert result["code"] == "MEETING_TTS_FAILED"
    playback.fetch.assert_not_awaited()
    playback.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_meeting_synthesis_rejects_a_different_browser_connection(playback):
    result = await playback.request(
        "meeting.speech.synthesize", {**playback.identity, "chunk_index": 0},
        connection=SimpleNamespace(_meeting_owner_id="other-owner"),
    )

    assert result["ok"] is False
    assert result["error"] == "Meeting playback is not owned by this connection"
    playback.fetch.assert_not_awaited()
    playback.synthesize.assert_not_awaited()
