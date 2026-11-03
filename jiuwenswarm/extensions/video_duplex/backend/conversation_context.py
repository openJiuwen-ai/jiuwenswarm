"""Import persisted realtime speech into the shared chat Agent before dispatch."""

import asyncio

from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
)

from jiuwenswarm.server.runtime.session import session_history

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


async def load_voice_history(session_id: str) -> list[dict]:
    """Read durable conversational speech, excluding delegated result echoes."""
    if not await asyncio.to_thread(session_history.flush_pending_writes, timeout=5):
        raise RuntimeError("Voice history is still saving; retry the request")
    records = await asyncio.to_thread(session_history.load_history_records, session_id)
    return [record for record in records if conversation_record_id(record)
            and isinstance(record.get("content"), str) and record["content"].strip()]


async def sync_conversation_context(agent, session_id):
    react = getattr(agent, "react_agent", None)
    if react is None:
        return
    context = react.context_engine.get_context(session_id=session_id)
    if context is None:
        return  # Cold creation restores disk history through warmup_session_context.
    records = await load_voice_history(session_id)
    imported = False
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
        message_type = UserMessage if record["role"] == "user" else AssistantMessage
        metadata = {HISTORY_ID: identity}
        if record["role"] == "user":
            metadata.update({
                OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
                OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: "video_duplex",
            })
        await context.add_messages(message_type(content=content, metadata=metadata))
        seen.add(identity)
        imported = True
    if imported:
        from jiuwenswarm.agents.harness.common.session_ops_service import resolve_live_agent_session

        session = resolve_live_agent_session(agent, session_id)
        if session is None:
            raise RuntimeError("The conversation session is unavailable; retry the message.")
        # ReAct.create_context reloads this snapshot before reading the next query.
        # Updating only the pooled context silently loses imported voice messages.
        await react.context_engine.save_contexts(session)
