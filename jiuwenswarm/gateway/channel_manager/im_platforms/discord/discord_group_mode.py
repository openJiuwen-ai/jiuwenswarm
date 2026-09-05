# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Group chat modes for the Discord channel (E4 - step 1).

Discord answers every message in a server channel today: the inbound
handler filters by guild, channel and allowlist, then hands anything with
text to the agent. In a shared channel that means the bot replies to
conversations it was never addressed in.

Telegram already solved this with ``group_chat_mode``; this module lifts
the same vocabulary so both platforms behave alike:

- ``all``     - every message in the channel
- ``mention`` - only messages mentioning the bot (default)
- ``reply``   - only replies to the bot's own messages
- ``off``     - nothing in server channels

Direct messages are never affected: a DM is addressed to the bot by
construction.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

GROUP_MODE_ALL = "all"
GROUP_MODE_MENTION = "mention"
GROUP_MODE_REPLY = "reply"
GROUP_MODE_OFF = "off"
GROUP_CHAT_MODES = (
    GROUP_MODE_ALL,
    GROUP_MODE_MENTION,
    GROUP_MODE_REPLY,
    GROUP_MODE_OFF,
)


def normalize_group_chat_mode(raw: str | None) -> str:
    """Return a supported mode, falling back to ``mention`` when unknown."""
    mode = str(raw or "").strip().lower()
    if mode in GROUP_CHAT_MODES:
        return mode
    if mode:
        logger.warning(
            "channels.discord.group_chat_mode=%r is not one of %s; using %r",
            raw, "/".join(GROUP_CHAT_MODES), GROUP_MODE_MENTION,
        )
    return GROUP_MODE_MENTION


def mentions_bot(text: str, bot_user_id: str) -> bool:
    """True when *text* carries a Discord mention of the bot."""
    if not bot_user_id:
        return False
    return re.search(rf"<@!?{re.escape(str(bot_user_id))}>", text or "") is not None


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove the bot mention so the agent sees the question only."""
    if not bot_user_id:
        return (text or "").strip()
    cleaned = re.sub(rf"<@!?{re.escape(str(bot_user_id))}>", " ", text or "")
    return " ".join(cleaned.split())


def should_handle(
    text: str,
    *,
    mode: str,
    bot_user_id: str,
    is_direct_message: bool,
    is_reply_to_bot: bool = False,
) -> bool:
    """Decide whether an inbound server message is addressed to the bot."""
    if is_direct_message:
        return True
    resolved = normalize_group_chat_mode(mode)
    if resolved == GROUP_MODE_OFF:
        return False
    if resolved == GROUP_MODE_ALL:
        return True
    if resolved == GROUP_MODE_REPLY:
        return is_reply_to_bot or mentions_bot(text, bot_user_id)
    return mentions_bot(text, bot_user_id)