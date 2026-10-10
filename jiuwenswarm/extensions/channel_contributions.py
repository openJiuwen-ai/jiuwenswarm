# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Channels contributed by installed extensions."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from jiuwenswarm.common.utils import logger
from jiuwenswarm.extensions import overrides

CHANNEL_KIND = "channels"


def _channel_id(channel: str | Enum) -> str:
    value = channel.value if isinstance(channel, Enum) else channel
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class ChannelSpec:
    channel: str | Enum
    config_fields: tuple[str, ...] | None = None
    delivery: Callable[..., Any] | None = None
    default_metadata: Callable[..., dict | None] | None = None
    factory: Callable[..., Any] | None = None
    check_relay: Callable[[str, dict], None] | None = None
    replaces: str = ""
    source: str = ""
    shared_im_input: bool = False

    @property
    def channel_id(self) -> str:
        return _channel_id(self.channel)


_CONTRIBUTED: dict[str, ChannelSpec] = {}


def register_channel_spec(spec: ChannelSpec) -> None:
    """Register a channel; replacing a built-in needs operator consent."""
    from jiuwenswarm.gateway.channel_manager.base import ChannelType

    channel_id = spec.channel_id
    if not channel_id.strip():
        raise ValueError("channel spec must declare a channel id")
    if not callable(spec.factory):
        raise ValueError(f"channel {channel_id!r} must declare a factory")
    if channel_id in _CONTRIBUTED:
        raise ValueError(f"channel {channel_id!r} is already contributed by {_CONTRIBUTED[channel_id].source}")
    builtin = channel_id in {channel.value for channel in ChannelType} | {"a2a", "feishu_enterprise"}
    if spec.replaces and spec.replaces != channel_id:
        raise ValueError(f"channel {channel_id!r} must use the id it replaces: {spec.replaces!r}")
    if builtin and not spec.replaces:
        raise ValueError(f"channel {channel_id!r} is built in; declare replaces: {channel_id}")
    if not builtin and spec.replaces:
        raise ValueError(f"channel {channel_id!r} is not built in; there is nothing to replace")
    if builtin:
        overrides.require_override_permitted(CHANNEL_KIND, channel_id, source=spec.source)
    _CONTRIBUTED[channel_id] = spec
    log = logger.warning if builtin else logger.info
    log("[channels] %s %s channel %s", spec.source or "an unnamed package", "replaces" if builtin else "contributes", channel_id)


def unregister_channel_spec(channel_id: str | Enum) -> ChannelSpec | None:
    """Remove a contribution; a replacement falls back to its built-in."""
    return _CONTRIBUTED.pop(_channel_id(channel_id), None)


def contributed_channel_ids() -> tuple[str, ...]:
    return tuple(_CONTRIBUTED)


def contributed_spec_for(channel: str | Enum | None) -> ChannelSpec | None:
    if channel is None:
        return None
    return _CONTRIBUTED.get(_channel_id(channel))
