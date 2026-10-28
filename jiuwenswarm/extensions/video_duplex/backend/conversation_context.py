"""Import persisted realtime speech into the shared chat Agent before dispatch."""

import asyncio

from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
)

from jiuwenswarm.server.runtime.session.session_history import (
    flush_pending_writes, load_history_records,
)

HISTORY_ID = "duplex_history_id"


def conversation_record_id(record):
    if record.get("channel_id") != "video_duplex":
        return None
    if record.get("role") == "user":
        return record.get("id") or record.get("request_id")
    if (
        record.get("role") == "assistant"
        and record.get("event_type") == "chat.final"
        and record.get("presentation") != "tool_result"
    ):
        return record.get("id") or record.get("request_id")
    return None


async def sync_conversation_context(agent, session_id):
    react = getattr(agent, "react_agent", None)
    if react is None:
        return
    context = react.context_engine.get_context(session_id=session_id)
    if context is None:
        return  # Cold creation restores disk history through warmup_session_context.
    if not await asyncio.to_thread(flush_pending_writes, timeout=5):
        raise RuntimeError("语音对话历史尚未保存，请稍后重试。")
    records = await asyncio.to_thread(load_history_records, session_id)
    # Retain consumed IDs even if Core compacts its message window.
    seen = getattr(context, "duplex_history_ids", None)
    if seen is None:
        seen = {
            (getattr(message, "metadata", None) or {}).get(HISTORY_ID)
            for message in context.get_messages()
        }
        context.duplex_history_ids = seen
    for record in records:
        identity = conversation_record_id(record)
        content = record.get("content")
        if not identity or identity in seen:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        message_type = UserMessage if record["role"] == "user" else AssistantMessage
        metadata = {HISTORY_ID: identity}
        if record["role"] == "user":
            metadata.update({
                OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
                OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: "video_duplex",
            })
        await context.add_messages(message_type(content=content, metadata=metadata))
        seen.add(identity)
