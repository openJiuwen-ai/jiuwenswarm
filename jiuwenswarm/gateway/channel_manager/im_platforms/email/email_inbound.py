# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inbound e-mail parsing for the SMTP/IMAP channel (E6 - step 3).

Turns a raw RFC822 message into what the agent needs: who wrote, which
conversation it belongs to, and the text the sender actually typed - the
quoted history left by the mail client is dropped (E6 step 1).

Pure functions over ``email.message.Message``: no IMAP connection here, so
the parsing is unit-tested without a mailbox.
"""
from __future__ import annotations

import logging
from email.header import decode_header, make_header
from email.message import Message as MailMessage
from email.utils import parseaddr

from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_format import (
    strip_quoted_reply,
    thread_key,
)

logger = logging.getLogger(__name__)


def decode_field(raw: str) -> str:
    """Decode an RFC2047 header (``=?utf-8?B?...?=``) into plain text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # noqa: BLE001
        return str(raw).strip()


def sender_address(mail: MailMessage) -> str:
    """Bare address of the sender, lowercased."""
    _name, address = parseaddr(mail.get("From", "") or "")
    return address.strip().lower()


def subject_of(mail: MailMessage) -> str:
    """Decoded subject line."""
    return decode_field(mail.get("Subject", "") or "")


def plain_text_body(mail: MailMessage) -> str:
    """Best-effort plain-text body, preferring ``text/plain`` parts."""
    if not mail.is_multipart():
        payload = mail.get_payload(decode=True)
        if payload is None:
            return str(mail.get_payload() or "")
        charset = mail.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    for part in mail.walk():
        if part.get_content_type() != "text/plain":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def parse_inbound(mail: MailMessage) -> dict[str, str] | None:
    """Extract sender, subject, session id and the text actually written.

    Returns ``None`` when the mail carries nothing usable - no sender or an
    empty body once the quoted history is removed.
    """
    sender = sender_address(mail)
    if not sender:
        return None
    subject = subject_of(mail)
    content = strip_quoted_reply(plain_text_body(mail))
    if not content:
        return None
    return {
        "sender": sender,
        "subject": subject,
        "session_id": thread_key(sender, subject),
        "content": content,
    }


def is_from_self(mail: MailMessage, own_address: str) -> bool:
    """True when the mail was sent by the bot itself - never answer those."""
    own = str(own_address or "").strip().lower()
    return bool(own) and sender_address(mail) == own