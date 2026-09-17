"""Unit tests for shared channel behavior."""

from dataclasses import dataclass, field

from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter


@dataclass
class _Config:
    allow_from: list[str] = field(default_factory=list)


class _Channel(BaseChannel):
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _channel(patterns: list[str]) -> _Channel:
    return _Channel(_Config(patterns), RobotMessageRouter())


def test_empty_allow_list_allows_everyone() -> None:
    assert _channel([]).is_allowed("any-user")


def test_exact_match_is_preserved() -> None:
    channel = _channel(["user-123"])

    assert channel.is_allowed("user-123")
    assert not channel.is_allowed("user-1234")


def test_shell_style_wildcards_match_complete_sender_id() -> None:
    channel = _channel(["team-*-admin", "user-??", "bot-[ab]"])

    assert channel.is_allowed("team-east-admin")
    assert channel.is_allowed("user-42")
    assert channel.is_allowed("bot-a")
    assert not channel.is_allowed("prefix-team-east-admin")
    assert not channel.is_allowed("bot-c")


def test_composite_sender_matches_each_part() -> None:
    channel = _channel(["staff-*"])

    assert channel.is_allowed("chat-1|staff-007")
    assert not channel.is_allowed("chat-1|guest-007")
