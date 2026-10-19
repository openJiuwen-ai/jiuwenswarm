"""Adapt a ChannelPlugin registry into openjiuwen's ImLearningSource."""

from __future__ import annotations

from openjiuwen.harness.personal_context.im import (
    ImLearningCursor,
    ImLearningMessage,
    ImLearningSource,
    ImLearningTarget,
    ImMessageBatch,
)

from jiuwenswarm.server.im.im_connector.registry import ConnectorRegistry
from jiuwenswarm.server.im.im_connector.types import ChannelTarget, FetchOptions, ImMessage, MessagePage


def _to_learning_message(message: ImMessage) -> ImLearningMessage:
    return ImLearningMessage(
        channel_id=message.channel_id,
        msg_id=message.msg_id,
        conversation_external_id=message.conversation_external_id,
        content_text=message.content_text,
        sent_at=message.sent_at,
        sender_account=message.sender_account,
        sender_name=message.sender_name,
        content_type=message.content_type,
        is_self=message.is_self,
    )


def _next_cursor(
    page: MessagePage,
    cursor: ImLearningCursor | None,
) -> ImLearningCursor | None:
    extra = dict(cursor.extra) if cursor is not None else {}
    extra.pop("page_token", None)
    count = cursor.count if cursor is not None else 50
    direction = cursor.query_direction if cursor is not None else 0
    if page.next_page_token:
        extra["page_token"] = page.next_page_token
        return ImLearningCursor(
            message_id=None,
            query_direction=direction,
            count=count,
            extra=extra,
        )
    if not page.messages:
        return None
    oldest = min(page.messages, key=lambda item: (item.sent_at, item.msg_id or ""))
    if not oldest.msg_id:
        return None
    return ImLearningCursor(
        message_id=oldest.msg_id,
        query_direction=0 if direction is None else direction,
        count=count,
        extra=extra,
    )


class ConnectorLearningSource:
    """Read-only wrapper: learning fetch never calls send_message."""

    def __init__(self, registry: ConnectorRegistry) -> None:
        self._registry = registry

    async def fetch_messages(
        self,
        target: ImLearningTarget,
        cursor: ImLearningCursor | None = None,
    ) -> ImMessageBatch:
        plugin = self._registry.require(target.channel_id)
        extra = dict(cursor.extra) if cursor is not None else {}
        page_token = extra.get("page_token")
        page_token = str(page_token).strip() if page_token else None
        options = FetchOptions(
            count=cursor.count if cursor is not None else 50,
            message_id=None if page_token else (cursor.message_id if cursor is not None else None),
            page_token=page_token,
            query_direction=cursor.query_direction if cursor is not None else None,
        )
        channel_target = ChannelTarget(
            kind=target.kind,
            external_id=target.external_id,
            title=target.title,
        )
        page = await plugin.fetch_messages(channel_target, options)
        mapped = tuple(_to_learning_message(item) for item in page.messages)
        return ImMessageBatch(messages=mapped, next_cursor=_next_cursor(page, cursor))


def as_im_learning_source(registry: ConnectorRegistry) -> ImLearningSource:
    return ConnectorLearningSource(registry)


__all__ = ["ConnectorLearningSource", "as_im_learning_source"]
