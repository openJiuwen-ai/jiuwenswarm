# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IMAP polling loop for the e-mail channel (E6 - step 4).

The mailbox is the inbound transport: every interval the loop fetches
unseen mails, parses them (E6 step 3) and hands the written text to the
agent, remembering which thread the session belongs to so the reply goes
back to the same conversation.

The IMAP client is injected as a small protocol, so the loop is unit-tested
without a server: an object exposing ``fetch_unseen()`` returning raw
RFC822 bytes.
"""
from __future__ import annotations

import asyncio
import email
import logging
from typing import Any, Protocol

from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_inbound import (
    is_from_self,
    parse_inbound,
)

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 30.0


class MailFetcher(Protocol):
    """Anything able to hand over the unseen mails as raw bytes."""

    def fetch_unseen(self) -> list[bytes]:  # pragma: no cover - protocol
        ...


async def poll_once(channel: Any, fetcher: MailFetcher) -> int:
    """Fetch, parse and dispatch the unseen mails. Returns how many landed."""
    try:
        raw_mails = fetcher.fetch_unseen()
    except Exception as exc:  # noqa: BLE001
        logger.warning("EmailChannel IMAP fetch failed: %s", exc)
        return 0

    delivered = 0
    for raw in raw_mails or []:
        try:
            mail = email.message_from_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EmailChannel could not read a mail: %s", exc)
            continue
        if is_from_self(mail, getattr(channel.config, "email_address", "")):
            continue
        parsed = parse_inbound(mail)
        if parsed is None:
            continue
        if not channel.is_allowed(parsed["sender"]):
            logger.info("EmailChannel rejected mail from %s", parsed["sender"])
            continue
        channel.remember_thread(parsed["session_id"], parsed["sender"], parsed["subject"])
        await channel.deliver_inbound(
            parsed["session_id"],
            parsed["content"],
            metadata={
                "email_sender": parsed["sender"],
                "email_subject": parsed["subject"],
            },
        )
        delivered += 1
    return delivered


async def poll_forever(
    channel: Any,
    fetcher: MailFetcher,
    interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Poll the mailbox until the channel stops."""
    while channel.is_running:
        await poll_once(channel, fetcher)
        await asyncio.sleep(interval)
