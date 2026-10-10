"""Unit tests for the BaseChannel abstract contract."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget


async def _start(self: BaseChannel) -> None:
    self._running = True


async def _stop(self: BaseChannel) -> None:
    self._running = False


async def _send(
    self: BaseChannel, msg: Message, *, routing_target: RoutingTarget | None = None
) -> None:
    self.sent = msg


class _ChannelWithoutStart(BaseChannel):
    stop = _stop
    send = _send


class _ChannelWithoutStop(BaseChannel):
    start = _start
    send = _send


class _ChannelWithoutSend(BaseChannel):
    start = _start
    stop = _stop


class _CompleteChannel(BaseChannel):
    start = _start
    stop = _stop
    send = _send


def test_send_is_part_of_the_abstract_contract() -> None:
    assert BaseChannel.__abstractmethods__ == frozenset({"start", "stop", "send"})


@pytest.mark.parametrize(
    ("channel_cls", "missing"),
    [
        (_ChannelWithoutStart, "start"),
        (_ChannelWithoutStop, "stop"),
        (_ChannelWithoutSend, "send"),
    ],
)
def test_omitting_a_lifecycle_method_fails_at_construction(
    channel_cls: type[BaseChannel], missing: str
) -> None:
    with pytest.raises(TypeError, match=missing):
        channel_cls(config=None, router=RobotMessageRouter())


def test_a_complete_channel_still_constructs() -> None:
    channel = _CompleteChannel(config=None, router=RobotMessageRouter())

    assert channel.is_running is False
