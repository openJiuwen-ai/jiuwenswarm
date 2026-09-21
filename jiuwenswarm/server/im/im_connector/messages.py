"""Normalize platform-specific raw dicts onto ImMessage."""

from __future__ import annotations

from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.types import ImMessage


def to_im_message(
    raw: dict[str, Any],
    *,
    channel_id: str,
    conversation_external_id: str,
    is_self_account: Optional[str] = None,
    is_self_accounts: Optional[set[str]] = None,
) -> ImMessage:
    sender = str(raw.get("sender") or "")
    content = str(raw.get("content") or "")
    sent_at = int(raw.get("serverSendTime") or 0)
    msg_id = str(raw.get("msgId") or "")
    content_type = raw.get("contentType")
    if isinstance(content_type, str):
        content_type = content_type or None
    else:
        content_type = None
    is_self: Optional[bool] = None
    accounts = {item for item in (is_self_accounts or set()) if item}
    if is_self_account:
        accounts.add(is_self_account)
    if accounts:
        is_self = sender in accounts
    direction = "outbound" if is_self is True else "inbound"
    return ImMessage(
        channel_id=channel_id,
        msg_id=msg_id,
        conversation_external_id=conversation_external_id,
        sender_account=sender or None,
        sender_name=str(raw.get("senderName") or sender or "") or None,
        content_text=content,
        sent_at=sent_at,
        content_type=content_type,
        direction=direction,
        is_self=is_self,
    )
