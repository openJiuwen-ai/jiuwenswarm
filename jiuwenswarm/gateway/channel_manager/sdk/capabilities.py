# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Declarative capability sheet for chat channels.

First building block of the E1 connector SDK. Each channel adapter
declares what its platform supports so that upper layers (e.g. the
message handler, E2) can adapt content instead of silently dropping it.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelCapabilities:
    """What a channel is able to render or do.

    All flags default to ``False`` (and limits to ``None``), so an
    adapter that declares nothing is treated as a plain-text channel.
    Instances are immutable: a capability sheet must never change at
    runtime.

    Attributes:
        buttons: Supports interactive buttons (e.g. Slack Block Kit,
            Telegram InlineKeyboardMarkup, Discord Components).
        streaming: Supports editing an already-sent message, enabling
            word-by-word streaming replies.
        file_upload: Supports sending file attachments.
        rich_text: Supports formatted text (bold, lists, code blocks).
        threads: Supports threaded replies.
        max_message_length: Maximum characters per message imposed by
            the platform (e.g. 2000 for Discord), or ``None`` when the
            platform imposes no practical limit.
    """

    buttons: bool = False
    streaming: bool = False
    file_upload: bool = False
    rich_text: bool = False
    threads: bool = False
    max_message_length: int | None = None