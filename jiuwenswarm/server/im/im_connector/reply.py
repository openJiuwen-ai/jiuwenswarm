"""Shared outgoing reply formatting for user-state IM connectors."""

from __future__ import annotations

from typing import Optional

REPLY_PREFIX_TEMPLATE = "来自 {} 的数字分身："


def format_outgoing_reply_text(
    content: str,
    *,
    account: Optional[str],
    mention_sender: bool = False,
    sender: Optional[str] = None,
    mention_token: Optional[str] = None,
    reply_prefix: Optional[str] = None,
    max_reply_chars: int = 1000,
) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    account_text = (account or "").strip()
    if reply_prefix is not None:
        prefix = reply_prefix
    else:
        prefix = REPLY_PREFIX_TEMPLATE.format(account_text) if account_text else ""
    if prefix and text.startswith(prefix):
        body = text
        full_prefix = ""
    else:
        body = text
        mention = ""
        sender_text = (sender or "").strip()
        if mention_sender and sender_text:
            mention = mention_token if mention_token is not None else f"@{sender_text} "
        full_prefix = f"{mention}{prefix}"
    max_chars = max(1, int(max_reply_chars or 1000))
    if len(full_prefix) >= max_chars:
        return full_prefix[:max_chars]
    remaining = max_chars - len(full_prefix)
    return f"{full_prefix}{body[:remaining]}"
