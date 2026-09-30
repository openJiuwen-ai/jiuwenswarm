# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SMTP e-mail channel (E6 - step 2).

Sends agent replies as plain-text mail. The capability sheet declares
nothing: e-mail has no buttons, no streaming, no rich text, so an
interactive card reaches the reader as the numbered-reply text produced by
the E2 pipeline.

Outbound only in this slice - inbound IMAP polling is the next one. The
SMTP client is injected so the channel is testable without a server.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_format import (
    reply_subject,
    thread_key,
)
from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT = "Assistant reply"


@dataclass
class EmailChannelConfig:
    """Settings for the SMTP channel (mirrors the email_settings block)."""

    enabled: bool = False
    email_address: str = ""
    token: str = ""
    smtp_server: str = "smtp.gmail.com"
    port: int = 587
    allow_from: list[str] = field(default_factory=list)
    default_recipient: str = ""


class EmailChannel(BaseChannel):
    """Deliver agent replies over SMTP."""

    name = "email"
    capabilities = ChannelCapabilities()

    def __init__(
        self,
        config: EmailChannelConfig,
        router: RobotMessageRouter,
        smtp_sender: Callable[[EmailMessage], Any] | None = None,
    ) -> None:
        super().__init__(config, router)
        self._smtp_sender = smtp_sender
        self._threads: dict[str, str] = {}

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("EmailChannel disabled, not starting")
            return
        if not self.config.email_address or not self.config.token:
            logger.warning("EmailChannel missing email_address or token, not starting")
            return
        self._running = True
        logger.info("EmailChannel ready as %s", self.config.email_address)

    async def stop(self) -> None:
        self._running = False
        self._threads.clear()

    def remember_thread(self, session_id: str, sender: str, subject: str) -> None:
        """Record which mail thread a session belongs to."""
        self._threads[str(session_id)] = f"{sender}|{subject}"

    def recipient_for(self, session_id: str) -> tuple[str, str]:
        """Return (address, subject) for a session, falling back to defaults."""
        stored = self._threads.get(str(session_id), "")
        if stored and "|" in stored:
            address, subject = stored.split("|", 1)
            return address, subject
        return self.config.default_recipient, DEFAULT_SUBJECT

    def build_mail(self, to_address: str, subject: str, body: str) -> EmailMessage:
        """Compose the plain-text mail an agent reply becomes."""
        mail = EmailMessage()
        mail["From"] = self.config.email_address
        mail["To"] = to_address
        mail["Subject"] = reply_subject(subject)
        mail.set_content(body)
        return mail

    async def send(
        self,
        msg: Message,
        *,
        routing_target: RoutingTarget | None = None,
    ) -> None:
        body = str((getattr(msg, "params", None) or {}).get("content", "") or "").strip()
        if not body:
            return
        to_address, subject = self.recipient_for(getattr(msg, "session_id", "") or "")
        if not to_address:
            logger.warning("EmailChannel has no recipient for session %s", msg.session_id)
            return
        mail = self.build_mail(to_address, subject, body)
        if self._smtp_sender is None:
            logger.warning("EmailChannel has no SMTP sender configured")
            return
        try:
            result = self._smtp_sender(mail)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("EmailChannel send failed: %s", exc)

    def session_id_for(self, sender: str, subject: str) -> str:
        """Session id of an inbound mail - one session per mail thread."""
        return thread_key(sender, subject)