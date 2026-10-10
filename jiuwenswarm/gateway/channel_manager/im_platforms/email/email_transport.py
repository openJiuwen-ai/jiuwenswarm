# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real SMTP and IMAP transports for the e-mail channel (E6 - step 5).

The previous slices kept the network out on purpose: the channel takes a
send callable and the poller takes a fetcher. This module supplies the two
real implementations, each opening a short-lived connection per operation -
simplest thing that survives a mailbox closing the socket while idle.
"""
from __future__ import annotations

import imaplib
import logging
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)

DEFAULT_IMAP_PORT = 993
FETCH_LIMIT = 20


class SmtpSender:
    """Send one mail per connection over STARTTLS."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password

    def __call__(self, mail: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=30) as server:
            server.starttls()
            server.login(self._username, self._password)
            server.send_message(mail)
        logger.info("EmailChannel delivered a mail to %s", mail.get("To", ""))


class ImapFetcher:
    """Fetch the unseen mails of a mailbox, newest last."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = DEFAULT_IMAP_PORT,
        mailbox: str = "INBOX",
        limit: int = FETCH_LIMIT,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._limit = int(limit)

    def fetch_unseen(self) -> list[bytes]:
        mails: list[bytes] = []
        box = imaplib.IMAP4_SSL(self._host, self._port)
        try:
            box.login(self._username, self._password)
            box.select(self._mailbox)
            status, data = box.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return mails
            for message_id in data[0].split()[-self._limit:]:
                status, payload = box.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload:
                    continue
                for part in payload:
                    if isinstance(part, tuple) and len(part) > 1:
                        mails.append(part[1])
                        break
        finally:
            try:
                box.logout()
            except Exception:  # noqa: BLE001
                pass
        return mails