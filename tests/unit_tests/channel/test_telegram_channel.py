"""Unit tests for the Telegram channel."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.telegram.telegram_connect import (
    TelegramChannel,
    TelegramChannelConfig,
)


class _FakeTelegramMessage:
    def __init__(self, message_id: int, text: str) -> None:
        self.message_id = message_id
        self.text = text
        self.reply_to_message = None
        self.reactions: list[str] = []

    async def set_reaction(self, reaction: str) -> None:
        self.reactions.append(reaction)


def _update(chat_id: int, message_id: int) -> Any:
    return SimpleNamespace(
        message=_FakeTelegramMessage(message_id, f"message-{message_id}"),
        effective_user=SimpleNamespace(id=1001, username="tester"),
        effective_chat=SimpleNamespace(
            id=chat_id,
            type="supergroup" if chat_id < 0 else "private",
        ),
    )


@pytest.mark.asyncio
async def test_handle_message_derives_session_without_retaining_chat_mapping() -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(enabled=True, group_chat_mode="all"),
        RobotMessageRouter(),
    )
    received: list[Message] = []
    channel.on_message(received.append)
    context = SimpleNamespace(bot=SimpleNamespace(username="test_bot", id=42))

    for message_id, chat_id in enumerate((123456, -987654, 123456), start=1):
        await channel._handle_message(_update(chat_id, message_id), context)

    assert [message.session_id for message in received] == [
        "telegram_123456",
        "telegram_-987654",
        "telegram_123456",
    ]
    assert not hasattr(channel, "_chat_sessions")
