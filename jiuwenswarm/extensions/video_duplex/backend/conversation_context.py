"""Import persisted realtime speech into the shared chat Agent before dispatch."""

import asyncio

from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
)

<<<<<<< HEAD
from jiuwenswarm.server.runtime.session import session_history
||||||| e487e9a30
from jiuwenswarm.server.runtime.session.session_history import (
    flush_pending_writes, load_history_records,
)
=======
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.server.runtime.session.history_replay import (
    HistoryReplayError,
    read_history_replay,
)
>>>>>>> origin/develop

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


<<<<<<< HEAD
async def load_voice_history(session_id: str) -> list[dict]:
    """Read durable conversational speech, excluding delegated result echoes."""
    if not await asyncio.to_thread(session_history.flush_pending_writes, timeout=5):
        raise RuntimeError("Voice history is still saving; retry the request")
    records = await asyncio.to_thread(session_history.load_history_records, session_id)
    return [record for record in records if conversation_record_id(record)
            and isinstance(record.get("content"), str) and record["content"].strip()]


||||||| e487e9a30
=======
def _is_voice_input(record):
    content = record.get("content")
    return bool(conversation_record_id(record) and isinstance(content, str) and content.strip())


async def _load_voice_batch(session_id, cursor=None):
    if not await asyncio.to_thread(session_history.flush_pending_writes, timeout=5):
        raise RuntimeError("Voice history is still saving; retry the request")
    return await asyncio.to_thread(read_history_replay, session_id, cursor=cursor,
                                   is_restorable=_is_voice_input, voice_only=True)


async def load_voice_history(session_id: str) -> list[dict]:
    """Read bounded durable conversational speech, excluding delegated result echoes."""
    return (await _load_voice_batch(session_id)).records


>>>>>>> origin/develop
async def sync_conversation_context(agent, session_id):
    react = getattr(agent, "react_agent", None)
    if react is None:
        return
    context = react.context_engine.get_context(session_id=session_id)
    if context is None:
        return  # Cold creation restores disk history through warmup_session_context.
<<<<<<< HEAD
    records = await load_voice_history(session_id)
    imported = False
||||||| e487e9a30
    if not await asyncio.to_thread(flush_pending_writes, timeout=5):
        raise RuntimeError("语音对话历史尚未保存，请稍后重试。")
    records = await asyncio.to_thread(load_history_records, session_id)
=======
    from jiuwenswarm.server.runtime.session.context_snapshot import context_snapshot_lock

    async with context_snapshot_lock(context):
        await _sync_conversation_context(agent, session_id, context, react)


async def _sync_conversation_context(agent, session_id, context, react):
    cursor = getattr(context, "duplex_history_cursor", None)
    batch = await _load_voice_batch(session_id, cursor)
    records = batch.records
>>>>>>> origin/develop
    # Retain consumed IDs even if Core compacts its message window.
    seen = getattr(context, "duplex_history_ids", None)
    if seen is None:
        seen = {
            (getattr(message, "metadata", None) or {}).get(HISTORY_ID)
            for message in context.get_messages()
        }
        context.duplex_history_ids = seen
    pending = []
    pending_ids = set(seen)
    for record in records:
        identity = conversation_record_id(record)
        content = record.get("content")
<<<<<<< HEAD
        if not identity or identity in seen:
            continue
||||||| e487e9a30
        if not identity or identity in seen:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
=======
        if not identity or identity in pending_ids:
            continue
>>>>>>> origin/develop
        message_type = UserMessage if record["role"] == "user" else AssistantMessage
        metadata = {HISTORY_ID: identity}
        if record["role"] == "user":
            metadata.update({
                OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
                OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: "video_duplex",
            })
<<<<<<< HEAD
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
||||||| e487e9a30
        await context.add_messages(message_type(content=content, metadata=metadata))
        seen.add(identity)
=======
        pending.append(message_type(content=content, metadata=metadata))
        pending_ids.add(identity)
    if pending:
        from jiuwenswarm.agents.harness.common.session_ops_service import resolve_live_agent_session

        session = resolve_live_agent_session(agent, session_id)
        if session is None:
            raise RuntimeError("The conversation session is unavailable; retry the message.")
        previous = context.save_state() if callable(getattr(context, "save_state", None)) else None
        from jiuwenswarm.server.runtime.session.context_snapshot import SNAPSHOT_KEY

        previous_reference = session.get_state(SNAPSHOT_KEY) if callable(getattr(session, "get_state", None)) else None
        recovery_fields = ("history_recovery_cursor", "history_recovery_covered", "history_recovery_recorded_ids",
                           "duplex_history_cursor")
        recovery_state = {name: getattr(context, name, None) for name in recovery_fields}
        saved_state = session.get_state("context") if callable(getattr(session, "get_state", None)) else None
        if isinstance(saved_state, dict):
            saved_state = {key: dict(value) for key, value in saved_state.items()}
        try:
            for message in pending:
                await context.add_messages(message)
            # ReAct.create_context reloads this snapshot before the next query.
            await react.context_engine.save_contexts(session)
        except BaseException as exc:
            if previous is not None:
                context.load_state({context.context_id(): previous})
            if callable(getattr(session, "update_state", None)):
                session.update_state({"context": None, SNAPSHOT_KEY: None})
                session.update_state({"context": saved_state, SNAPSHOT_KEY: previous_reference})
            for name, value in recovery_state.items():
                if value is None:
                    context.__dict__.pop(name, None)
                else:
                    setattr(context, name, value)
            if not isinstance(exc, Exception):
                raise
            raise HistoryReplayError("Could not save imported voice context") from exc
    seen.update(conversation_record_id(record) for record in records)
    # Never advance before both import and the ReAct reload snapshot succeeded.
    cursor = batch.cursor
    context.duplex_history_cursor = cursor if cursor is not None and cursor.path.suffix.lower() == ".jsonl" else None
    if context.duplex_history_cursor is not None:
        # The durable cursor fences all older imports, including messages that
        # Core has compacted. Keep only IDs still present in the active window.
        retained_ids = {(getattr(message, "metadata", None) or {}).get(HISTORY_ID)
                        for message in context.get_messages()}
        seen.intersection_update(retained_ids)
>>>>>>> origin/develop
