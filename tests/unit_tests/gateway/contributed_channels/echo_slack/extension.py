# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Stub package contributing a channel, in place of a real connector package.

Shaped like what a package shipping a connector does: declare the channel's
identity, its gateway-side facts and the factory that builds its
``BaseChannel`` subclass, then let the host construct and own it.

``replaces`` is the package's half of a replacement. The operator's half is
``channels.slack`` under ``extensions.allow_overrides``. Without it the
registration is refused.
"""

from typing import Any

from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter
from jiuwenswarm.extensions.channel_contributions import (
    ChannelSpec,
    register_channel_spec,
)
from jiuwenswarm.gateway.routing.keys import DeliveryTarget, SlackDeliveryTarget

SOURCE = "echo-slack 0.0.1"


class EchoSlackChannel(BaseChannel):
    """Records what it was asked to send instead of reaching a chat service."""

    name = "slack"

    def __init__(self, config: dict, router: RobotMessageRouter):
        super().__init__(config, router)
        self.channel_id = "slack"
        self.app_id = str(config.get("app_id") or "")
        self.sent: list[Any] = []

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: Any, *, routing_target: Any = None) -> None:
        self.sent.append(msg)


def _build(config: dict, router: RobotMessageRouter) -> list[BaseChannel]:
    """One instance per configured bot token, as a workspace connector would."""
    return [EchoSlackChannel(config, router)]


def _delivery(channel_id: str, *, chat_id: str = "", **kwargs: Any) -> DeliveryTarget:
    return SlackDeliveryTarget(channel_id=channel_id, target_channel_id=chat_id)


SPEC = ChannelSpec(
    channel="slack",
    config_fields=("bot_token",),
    delivery=_delivery,
    factory=_build,
    replaces="slack",
    source=SOURCE,
)


async def register_extensions(registry: Any) -> None:
    register_channel_spec(SPEC)
