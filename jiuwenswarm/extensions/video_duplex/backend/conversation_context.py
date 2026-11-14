"""Import persisted realtime speech into the shared chat Agent before dispatch."""

import asyncio
import logging

from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
)

from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.server.runtime.session.history_replay import (
    HistoryReplayError,
    read_history_replay,
)

logger = logging.getLogger(__name__)

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


async def sync_conversation_context(agent, session_id):
    react = getattr(agent, "react_agent", None)
    if react is None:
        return
    context = react.context_engine.get_context(session_id=session_id)
    if context is None:
        return  # Cold creation restores disk history through warmup_session_context.
    from jiuwenswarm.server.runtime.session.context_snapshot import context_snapshot_lock

    async with context_snapshot_lock(context):
        await _sync_conversation_context(agent, session_id, context, react)


async def _load_voice_batch_tolerant(session_id, cursor):
    """Read one voice batch, or ``None`` when the history writer is busy.

    Ordinary chat dispatch runs this sync for every message, and the voice
    import only enriches the context: a flush timeout (RuntimeError raised by
    ``_load_voice_batch``) must not fail the whole chat request (#6608). The
    caller leaves its cursor untouched, so a later sync picks the records up.
    """
    try:
        return await _load_voice_batch(session_id, cursor)
    except RuntimeError as exc:
        logger.warning("Skipping voice history import for %s: %s", session_id, exc)
        return None


def _pending_voice_messages(context, records):
    """Build the not-yet-imported voice messages from one batch.

    Returns ``(pending, seen_ids)`` where ``seen_ids`` is the retained
    consumed-ID set (also cached on the context). IDs already seen are skipped
    so a warm-restored message is never imported twice.
    """
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
        if not identity or identity in pending_ids:
            continue
        message_type = UserMessage if record["role"] == "user" else AssistantMessage
        metadata = {HISTORY_ID: identity}
        if record["role"] == "user":
            metadata.update({
                OPENJIUWEN_MESSAGE_ORIGIN_METADATA: OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
                OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: "video_duplex",
            })
        pending.append(message_type(content=content, metadata=metadata))
        pending_ids.add(identity)
    return pending, seen


async def _sync_conversation_context(agent, session_id, context, react):
    cursor = getattr(context, "duplex_history_cursor", None)
    batch = await _load_voice_batch_tolerant(session_id, cursor)
    if batch is None:
        return
    records = batch.records
    pending, seen = _pending_voice_messages(context, records)
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
    _advance_voice_cursor(context, seen, batch)


def _advance_voice_cursor(context, seen, batch):
    """Persist the batch cursor and trim consumed IDs it now fences.

    The durable cursor fences all older imports, including messages that Core
    has compacted; only IDs still present in the active window are kept.
    """
    # Never advance before both import and the ReAct reload snapshot succeeded.
    cursor = batch.cursor
    context.duplex_history_cursor = cursor if cursor is not None and cursor.path.suffix.lower() == ".jsonl" else None
    if context.duplex_history_cursor is not None:
        retained_ids = {(getattr(message, "metadata", None) or {}).get(HISTORY_ID)
                        for message in context.get_messages()}
        seen.intersection_update(retained_ids)
