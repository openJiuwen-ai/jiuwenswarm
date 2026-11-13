"""Full-duplex titles use native history metadata and native session notifications."""
# pylint: disable=protected-access

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_live
from jiuwenswarm.server.runtime.session import session_history, session_metadata


class Channel:
    def __init__(self):
        self.handlers = {}
        self.events = []

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
    monkeypatch.setattr(session_history, "_enqueue_history_item", lambda *_args, **_kwargs: None)
    channel = Channel()
    video_live.register_video_live_handler(channel, agent_client=None, normalize_media_attachments=lambda value: value)
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
