
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Email formatting helpers for the SMTP/IMAP channel (E6 - step 1).

Email is the plainest channel there is: no buttons, no streaming, no
threads API. It is therefore the sharpest test of the connector SDK - an
interactive card must arrive as readable numbered-reply text, and a reply
must be recognised as an answer to the question that was asked.

Pure helpers, no network: subject threading (``Re:`` handling and the
conversation key), quoted-history stripping.
"""
from __future__ import annotations

import re

RE_PREFIX = re.compile(r"^\s*(re|fw|fwd|rép|ref)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)
QUOTE_MARKERS = (
    "-----original message-----",
    "________________________________",
    "-------- forwarded message --------",
)
ON_WROTE = re.compile(
    r"^\s*(on .+ wrote:|le .+ a écrit\s*:|.+ <[^>]+> wrote:)\s*$",
    re.IGNORECASE,
)


def normalize_subject(subject: str) -> str:
    """Strip every ``Re:``/``Fwd:`` prefix so a thread keeps one key."""
    text = str(subject or "").strip()
    previous = None
    while previous != text:
        previous = text
        text = RE_PREFIX.sub("", text).strip()
    return text


def reply_subject(subject: str) -> str:
    """Build the subject of a reply without stacking prefixes."""
    base = normalize_subject(subject)
    return f"Re: {base}" if base else "Re:"


def thread_key(sender: str, subject: str) -> str:
    """Conversation key for a mail: one thread per sender and subject."""
    address = str(sender or "").strip().lower()
    return f"{address}|{normalize_subject(subject).lower()}"


def strip_quoted_reply(body: str) -> str:
    """Keep only what the sender wrote, dropping the quoted history."""
    lines = str(body or "").splitlines()
    kept: list[str] = []
    for line in lines:
        lowered = line.strip().lower()
        if lowered in QUOTE_MARKERS or ON_WROTE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()