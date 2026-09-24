"""User-state IM connectors (ChannelPlugin + platform CLI implementations)."""

from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin
from jiuwenswarm.server.im.im_connector.registry import ConnectorRegistry
from jiuwenswarm.server.im.im_connector.types import (
    ChannelMeta,
    ChannelPerson,
    ChannelTarget,
    FetchOptions,
    Identity,
    ImConversation,
    ImMessage,
    MessagePage,
    ReplyContext,
    SendOptions,
    SendResult,
    TestResult,
)

__all__ = [
    "ChannelMeta",
    "ChannelPerson",
    "ChannelPlugin",
    "ChannelTarget",
    "ConnectorRegistry",
    "FetchOptions",
    "Identity",
    "ImConversation",
    "ImMessage",
    "MessagePage",
    "ReplyContext",
    "SendOptions",
    "SendResult",
    "TestResult",
]
