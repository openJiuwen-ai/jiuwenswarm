# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wiring tests for Discord group chat modes (E4 step 2)."""
import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.discord.discord_connect import (
    DiscordChannel,
    DiscordChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

class _Author:
    def __init__(self, uid: str = "U1", bot: bool = False) -> None:
        self.id = uid
        self.bot = bot
        self.name = "user"
        self.global_name = "User"


class _Message:
    def __init__(self, content: str, in_guild: bool = True, reply_author: str = "") -> None:
        self.content = content
        self.author = _Author()
        self.id = "M1"
        self.guild = type("G", (), {"id": "G1", "name": "guild"})() if in_guild else None
        self.channel = type("C", (), {"id": "C1", "name": "chan"})()
        self.reference = None
        if reply_author:
            author = type("A", (), {"id": reply_author})()
            resolved = type("X", (), {"author": author})()
            self.reference = type("R", (), {"resolved": resolved})()


def _channel(mode: str = "mention") -> tuple[DiscordChannel, list]:
    channel = DiscordChannel(
        DiscordChannelConfig(enabled=True, group_chat_mode=mode),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = type("Cl", (), {"user": type("U", (), {"id": "999"})()})()
    received: list = []
    channel.on_message(received.append)
    return channel, received


async def _noop(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def _skip_reactions(monkeypatch):
    monkeypatch.setattr(DiscordChannel, "_add_reaction_emoji", _noop, raising=False)


async def test_plain_channel_message_is_ignored_by_default():
    channel, received = _channel()
    await channel._handle_discord_message(_Message("lunch at noon?"))
    assert received == []


async def test_mentioned_message_is_handled_and_mention_stripped():
    channel, received = _channel()
    await channel._handle_discord_message(_Message("<@999> summarize this"))
    assert len(received) == 1
    assert received[0].params["content"] == "summarize this"


async def test_direct_message_is_always_handled():
    channel, received = _channel()
    await channel._handle_discord_message(_Message("hello", in_guild=False))
    assert len(received) == 1


async def test_all_mode_handles_every_message():
    channel, received = _channel("all")
    await channel._handle_discord_message(_Message("lunch?"))
    assert len(received) == 1


async def test_off_mode_ignores_even_mentions():
    channel, received = _channel("off")
    await channel._handle_discord_message(_Message("<@999> ping"))
    assert received == []


async def test_reply_mode_handles_a_reply_to_the_bot():
    channel, received = _channel("reply")
    await channel._handle_discord_message(_Message("yes please", reply_author="999"))
    assert len(received) == 1


async def test_reply_to_another_user_is_ignored_in_reply_mode():
    channel, received = _channel("reply")
    await channel._handle_discord_message(_Message("yes please", reply_author="123"))
    assert received == []


async def test_mention_only_message_is_not_forwarded():
    channel, received = _channel()
    await channel._handle_discord_message(_Message("<@999>"))
    assert received == []