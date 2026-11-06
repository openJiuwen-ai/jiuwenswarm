# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persist Full-duplex conversation events in the AgentServer session store."""

import asyncio
import math
import threading

from agent_gateway.common.protocol_ids import validate_session_id
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.context import RequestContext

# The history writer queues appends. Serialize the check and durable append
# for a session so a concurrent retry cannot enqueue the same event twice.
_SESSION_LOCKS = tuple(threading.Lock() for _ in range(64))
_EVENT_FIELDS = {
    None: (),
    "chat.final": ("presentation",),
    "chat.reasoning": ("reasoning_content", "reasoning_started_at", "reasoning_updated_at"),
    "chat.tool_call": ("tool_call",),
    "chat.tool_result": ("tool_result",),
    "chat.file": ("files",),
}


def append_video_conversation(request: AgentRequest) -> dict:
    from jiuwenswarm.server.runtime.session import session_history, session_metadata
    from jiuwenswarm.server.runtime.session.lifecycle import guard

    session = validate_session_id(request.session_id)
    params = request.params or {}
    event_id = params.get("event_id")
    role = params.get("role")
    content = params.get("content")
    event_type = params.get("event_type")
    timestamp = params.get("timestamp")
    extra = params.get("extra")
    invalid_event = "Invalid Full-duplex conversation event"
    if session == "new" or role not in ("user", "assistant"):
        raise ValueError(invalid_event)
    if not isinstance(event_id, str):
        raise ValueError(invalid_event)
    if not event_id.strip() or len(event_id) > 160:
        raise ValueError(invalid_event)
    if not isinstance(content, str) or len(content) > 100_000:
        raise ValueError(invalid_event)
    if event_type is not None and not isinstance(event_type, str):
        raise ValueError(invalid_event)
    if event_type not in _EVENT_FIELDS:
        raise ValueError(invalid_event)
    if not isinstance(timestamp, (int, float)):
        raise ValueError(invalid_event)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError(invalid_event)
    if extra is not None and not isinstance(extra, dict):
        raise ValueError(invalid_event)

    safe_extra = {key: extra[key] for key in _EVENT_FIELDS[event_type] if extra and key in extra}
    request_id = f"video-duplex-{event_id}"
    with _SESSION_LOCKS[hash(session) % len(_SESSION_LOCKS)]:
        guard(session)
        metadata = session_metadata.get_session_metadata(
            session, cache_bust=True, enable_writeback=False,
        )
        if not metadata or str(metadata.get("user_id") or "").strip() != (request.user_id or ""):
            raise ValueError("Conversation is unavailable to this user")
        if not session_history.flush_pending_writes(timeout=5):
            raise TimeoutError("Conversation history is not yet durable")
        if any(
            record.get("request_id") == request_id
            for record in session_history.load_history_records(session)
        ):
            return {}

        previous_title = str(metadata.get("title") or "")
        if role == "user" and content.strip() and previous_title == "Full-duplex conversation":
            session_metadata.update_session_metadata(
                session_id=session, clear_title=True, user_content=content,
                touch_last_message_at=False, sync_write=True,
            )
        session_history.append_history_record(
            session_id=session, request_id=request_id, channel_id="video_duplex",
            role=role, content=content, timestamp=timestamp, event_type=event_type,
            extra=safe_extra or None,
            mode=str(metadata.get("mode") or ("team.design" if metadata.get("work_mode") == "design" else "agent")),
        )
        if not session_history.flush_pending_writes(timeout=5):
            raise TimeoutError("Conversation history is not yet durable")
        if role == "user" and content.strip():
            title = str(session_metadata.get_session_metadata(session).get("title") or "")
            if title and title != previous_title:
                return {"updated_title": title}
        return {}


async def handle_video_conversation_append(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        payload = await asyncio.to_thread(append_video_conversation, request)
        ok = True
    except (ValueError, TimeoutError) as exc:
        payload = {"error": str(exc), "code": "VIDEO_HISTORY_UNAVAILABLE"}
        ok = False
    await ctx.sink.send_response(AgentResponse(
        request_id=request.request_id, channel_id=request.channel_id, ok=ok, payload=payload,
    ))
