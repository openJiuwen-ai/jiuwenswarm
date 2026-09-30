# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for XiaoyiChannel._send_legacy streaming flags.

Regression: the streaming branch contained dead code (incremental_text was
computed but never sent; `last_chunk = last_chunk` self-assignment; `final`
assigned twice). This test pins the actual wire behaviour: append/lastChunk/
final and accumulated-text bookkeeping must stay unchanged.
"""

from __future__ import annotations

import asyncio

from jiuwenswarm.common.schema.message import EventType, Message, Mode
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    XiaoyiChannel,
    XiaoyiChannelConfig,
)


class _Router:
    def __init__(self) -> None:
        self.routed = []

    async def handle_agent_response(self, *args, **kwargs) -> None:
        self.routed.append((args, kwargs))


def _make_channel() -> XiaoyiChannel:
    cfg = XiaoyiChannelConfig(enable_streaming=True)
    ch = XiaoyiChannel(cfg, _Router())  # type: ignore[arg-type]
    ch._ws_connections = {"url1": object()}
    return ch


def _msg(event_type: EventType, content: str, is_complete: bool = False) -> Message:
    return Message(
        id="task-1",
        type="event",
        channel_id="xiaoyi",
        session_id="sess-1",
        params={},
        timestamp=0.0,
        ok=True,
        payload={"content": content, "is_complete": is_complete},
        event_type=event_type,
        mode=Mode.AGENT,
        metadata={"xiaoyi_session_id": "sess-1", "xiaoyi_task_id": "task-1"},
    )


async def test_delta_message_sets_streaming_flags() -> None:
    """A CHAT_DELTA chunk must be sent with append=True, last_chunk=False."""
    ch = _make_channel()
    sent: dict = {}

    async def fake_send(session_id, task_id, text, url_key, *, append, last_chunk, is_final):
        sent.update(
            text=text, append=append, last_chunk=last_chunk, is_final=is_final
        )

    ch._send_text_response = fake_send  # type: ignore[method-assign]
    await ch._send_legacy(_msg(EventType.CHAT_DELTA, "你好"))
    assert sent["append"] is True
    assert sent["last_chunk"] is False
    assert sent["is_final"] is False


async def test_final_message_sets_last_chunk() -> None:
    """CHAT_FINAL with is_complete must be sent as the final chunk."""
    ch = _make_channel()
    sent: dict = {}

    async def fake_send(session_id, task_id, text, url_key, *, append, last_chunk, is_final):
        sent.update(
            text=text, append=append, last_chunk=last_chunk, is_final=is_final
        )

    ch._send_text_response = fake_send  # type: ignore[method-assign]
    await ch._send_legacy(_msg(EventType.CHAT_FINAL, "你好，我是小艺", is_complete=True))
    assert sent["append"] is True
    assert sent["last_chunk"] is True
    assert sent["is_final"] is True


async def test_accumulated_text_is_current_chunk() -> None:
    """Accumulated text (used for push notification) must be the current chunk,
    and cleared when the session finalises."""
    ch = _make_channel()
    await ch._send_legacy(_msg(EventType.CHAT_DELTA, "第一段"))
    assert ch._accumulated_texts["sess-1"] == "第一段"
    await ch._send_legacy(_msg(EventType.CHAT_FINAL, "第一段第二段", is_complete=True))
    assert "sess-1" not in ch._accumulated_texts
