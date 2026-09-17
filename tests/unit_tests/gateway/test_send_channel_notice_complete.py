# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for MessageHandler.send_channel_notice is_complete defaults.

Issue #300: 小艺 CLI 指令（/mode agent、/new_session 等）回包若 is_complete=False，
WS 只下发 lastChunk 而不带 final=true，界面会一直处于「处理中」直到超时。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _FakeAgentClient:
    @staticmethod
    async def send_request(env: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True, payload={})

    @staticmethod
    async def send_request_stream(env: object):
        if False:  # pragma: no cover
            yield env


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        handler = cls(_FakeAgentClient())
        handler.published = []
        return handler

    async def publish_robot_messages(self, msg: object) -> None:
        self.published.append(msg)


def _user_infos() -> dict:
    return {
        "id": "req-notice-1",
        "meta_data": {"xiaoyi_session_id": "xy-sess", "xiaoyi_task_id": "xy-task"},
        "app_id": "app-1",
    }


@pytest.mark.asyncio
async def test_send_channel_notice_xiaoyi_defaults_is_complete_true() -> None:
    """CLI notice to xiaoyi must complete so UI exits processing (Issue #300)."""
    handler = _TestMessageHandler.create()

    await handler.send_channel_notice(
        _user_infos(),
        "xiaoyi",
        "sess-1",
        "[收到 CLI 指令], mode 已变更为 agent",
    )

    assert len(handler.published) == 1
    msg = handler.published[0]
    assert msg.channel_id == "xiaoyi"
    assert msg.event_type == EventType.CHAT_FINAL
    assert msg.payload["content"] == "[收到 CLI 指令], mode 已变更为 agent"
    assert msg.payload["is_complete"] is True


@pytest.mark.asyncio
async def test_send_channel_notice_other_channel_defaults_is_complete_true() -> None:
    handler = _TestMessageHandler.create()

    await handler.send_channel_notice(
        _user_infos(),
        "feishu",
        "sess-1",
        "ok",
    )

    assert handler.published[0].payload["is_complete"] is True


@pytest.mark.asyncio
async def test_send_channel_notice_dict_setdefault_preserves_explicit_false() -> None:
    """Callers that need multi-frame notices can still pass is_complete=False."""
    handler = _TestMessageHandler.create()

    await handler.send_channel_notice(
        _user_infos(),
        "xiaoyi",
        "sess-1",
        {"content": "partial", "is_complete": False},
    )

    assert handler.published[0].payload["is_complete"] is False
    assert handler.published[0].payload["content"] == "partial"


@pytest.mark.asyncio
async def test_send_channel_notice_dict_fills_missing_is_complete_true() -> None:
    handler = _TestMessageHandler.create()

    await handler.send_channel_notice(
        _user_infos(),
        "xiaoyi",
        "sess-1",
        {"content": "skills list"},
    )

    assert handler.published[0].payload["is_complete"] is True
