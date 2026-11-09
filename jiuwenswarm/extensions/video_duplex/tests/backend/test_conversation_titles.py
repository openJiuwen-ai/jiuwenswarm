"""Full-duplex titles use native history metadata and native session notifications."""
# pylint: disable=protected-access

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.server.handlers.video_conversation import append_video_conversation

from jiuwenswarm.extensions.video_duplex.backend import video_live
from jiuwenswarm.server.runtime.session import session_history, session_metadata


class Channel:
    def __init__(self):
        self.handlers = {}
        self.events = []
        self.records = []

    def register_method(self, method, handler, **_kwargs):
        self.handlers[method] = handler

    async def send_response(self, _ws, _req_id, **response):
        assert response["ok"] is True

    async def send_event(self, _ws, event, payload):
        self.events.append((event, payload))


@pytest.fixture
def channel(tmp_path, monkeypatch):
    session_metadata._METADATA_QUEUE.join()
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "_METADATA_CACHE", {})
    channel = Channel()
    records = channel.records
    monkeypatch.setattr(session_history, "_enqueue_history_item", lambda _sid, record, **_: records.append(record))
    monkeypatch.setattr(session_history, "load_history_records", lambda _sid: records)

    async def send_request(envelope):
        payload = append_video_conversation(e2a_to_agent_request(envelope))
        return SimpleNamespace(ok=True, payload=payload)

    video_live.register_video_live_handler(
        channel, agent_client=SimpleNamespace(send_request=send_request),
        normalize_media_attachments=lambda value: value,
    )
    yield channel
    session_metadata._METADATA_QUEUE.join()


async def append(channel, role, content, event_id="first"):
    await channel.handlers["video.conversation.append"](
        object(), "request", {
            "session_id": "title-test", "event_id": event_id, "kind": role,
            "content": content, "timestamp": 1_800_000_000,
        }, "transport-session",
    )
    session_metadata._METADATA_QUEUE.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_title", ["", "Full-duplex conversation"])
async def test_first_user_turn_generates_and_publishes_native_title(channel, initial_title):
    session_metadata.init_session_metadata(session_id="title-test", channel_id="web", title=initial_title)
    await append(channel, "assistant", "你好，有什么可以帮助你？", "greeting")
    assert channel.events == []
    await append(channel, "user", "<context>内部信息</context>请帮我分析\n这道题")
    assert session_metadata.get_session_metadata("title-test", cache_bust=True)["title"] == "请帮我分析 这道题"
    assert channel.events == [("session.updated", {
        "session_id": "title-test", "title": "请帮我分析 这道题", "display_title": "请帮我分析 这道题",
    })]
    await append(channel, "user", "再查看香港天气", "second")
    assert len(channel.events) == 1
    assert session_metadata.get_session_metadata("title-test")["title"] == "请帮我分析 这道题"


@pytest.mark.asyncio
async def test_native_title_length_limit_is_reused(channel):
    session_metadata.init_session_metadata(session_id="title-test", channel_id="web")
    await append(channel, "user", "研究算法" * 30)
    assert session_metadata.get_session_metadata("title-test")["title"] == ("研究算法" * 30)[:50] + "..."


@pytest.mark.asyncio
async def test_rename_from_another_process_is_not_overwritten(channel):
    session_metadata.init_session_metadata(session_id="title-test", channel_id="web", title="Full-duplex conversation")
    stale = session_metadata.get_session_metadata("title-test")
    session_metadata._METADATA_QUEUE.join()
    session_metadata._write_metadata_sync("title-test", {**stale, "title": "我的算法笔记"})
    await append(channel, "user", "这道题怎么做")
    assert session_metadata.get_session_metadata("title-test", cache_bust=True)["title"] == "我的算法笔记"
    assert channel.events == []


@pytest.mark.asyncio
async def test_retry_does_not_append_history_twice(channel):
    session_metadata.init_session_metadata(session_id="title-test", channel_id="web")
    await append(channel, "user", "first turn", "same-event")
    await append(channel, "user", "first turn", "same-event")
    assert len(channel.records) == 1
    assert len(channel.events) == 1


@pytest.mark.asyncio
async def test_runtime_http_append_uses_session_owner_and_deduplicates(channel):
    import httpx

    from openjiuwen_protocol.e2a.models import E2AEnvelope
    from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import assemble_rest_request
    from jiuwenswarm.server.agent_http_routes import build_http_app
    from jiuwenswarm.server.app_services import AppServices

    session_metadata.init_session_metadata(session_id="title-test", channel_id="web", user_id="alice")
    params = {
        "event_id": "http-event", "role": "user", "content": "first turn",
        "timestamp": 1_800_000_000,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_http_app(AppServices()))) as client:
        for user in ("bob", "alice", "alice"):
            req = assemble_rest_request(
                E2AEnvelope(
                    method="video.conversation.append", request_id="http-test", session_id="title-test",
                    user_id=user, channel="video_tool", params=params,
                ),
                base_url="http://agent",
            )
            response = await client.request(req.verb, req.url, headers=req.headers, json=req.json_body)
            assert response.json()["ok"] is (user == "alice"), response.text

    assert len(channel.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected", [("agent", "agent.work.normal"), ("team.design", "team.design")])
async def test_e2a_history_append_preserves_conversation_mode(channel, mode, expected):
    session_metadata.init_session_metadata(
        session_id="title-test", channel_id="web", mode=mode,
    )
    await append(channel, "user", "Keep this conversation mode")
    assert channel.records[-1]["mode"] == expected
