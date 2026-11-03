# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-only native message handles and the compaction EventBus boundary."""

from __future__ import annotations

import asyncio
import copy
import json
from contextvars import ContextVar
from dataclasses import dataclass, replace
import uuid

from openjiuwen.core.context_engine import CompactionCandidate, CompactionError, CompactionSkipped
from openjiuwen.core.events import Event
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage, UserMessage

from jiuwenswarm.extensions_v2.event_types.session_compact import (
    SESSION_COMPACT_EVENT, SessionCompactInput, SessionCompacted, SessionCompactSkipped,
    valid_compact_input, valid_compact_output,
)


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(item if isinstance(item, str) else str(item.get("text", "")) for item in content or [])


class CompactMessageCodec:
    """Only the current invocation can resolve its complete native messages."""

    def __init__(self):
        self.handles = {}

    def encode(self, messages):
        views = []
        results = {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}
        for message in messages:
            handle = uuid.uuid4().hex
            self.handles[handle] = copy.deepcopy(message)
            view = {"role": "assistant" if message.role == "assistant" else "user",
                    "text": _text(message.content), "toolUses": [], "handle": handle}
            for call in getattr(message, "tool_calls", None) or []:
                try:
                    arguments = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
                except (ValueError, TypeError):
                    arguments = {"raw": call.arguments}
                item = {"tool_use_id": call.id, "tool": call.name, "input": arguments if isinstance(arguments, dict) else {"value": arguments}}
                if call.id in results:
                    item["text"] = _text(results[call.id].content)
                view["toolUses"].append(item)
            if isinstance(message, ToolMessage):
                view["text"] = ""
                view["toolResults"] = [{"tool_use_id": message.tool_call_id, "text": _text(message.content),
                                        "isError": message.metadata.get("isError") is True or message.metadata.get("is_error") is True}]
            views.append(view)
        return views

    def decode(self, messages):
        native = []
        used = set()
        for message in messages:
            handle = message.get("handle")
            if handle is not None:
                if handle in used or handle not in self.handles:
                    raise CompactionError("COMPACT_INVALID_RESULT")
                used.add(handle)
                native.append(copy.deepcopy(self.handles[handle]))
            elif message["role"] == "assistant":
                calls = [ToolCall(id=item["tool_use_id"], name=item["tool"], type="function",
                                  arguments=json.dumps(item["input"], ensure_ascii=False)) for item in message["toolUses"]]
                native.append(AssistantMessage(content=message["text"], tool_calls=calls or None))
            else:
                if message["text"] or not message.get("toolResults"):
                    native.append(UserMessage(content=message["text"]))
                for item in message.get("toolResults", []):
                    native.append(ToolMessage(tool_call_id=item["tool_use_id"], content=item["text"],
                                              metadata={"isError": item["isError"]}))
        return tuple(native)


@dataclass
class _CompactFrame:
    original: SessionCompactInput
    request: object
    context: object
    continuation: object
    codec: CompactMessageCodec
    owner: object
    active: bool = True


_FRAME = ContextVar("session_compact_invocation", default=None)


def compact_invocation():
    frame = _FRAME.get()
    return frame if frame is not None and frame.active and frame.owner is asyncio.current_task() else None


async def prepare_compact(event, ops):
    frame = compact_invocation()
    if frame is None or ops is None or ops.session_context != frame.context or not valid_compact_input(event.event_input):
        raise CompactionError("COMPACT_UNBOUND_INVOCATION")
    value = event.event_input
    request = replace(frame.request, messages=frame.codec.decode(value.messages), instructions=value.instructions)
    result = await frame.continuation(request)
    if isinstance(result, CompactionSkipped):
        return SessionCompactSkipped(result.skip)
    return SessionCompacted(frame.codec.encode(result.messages), result.tokens_before, result.tokens_after)


async def dispatch_compaction(bus, context, request, continuation):
    codec = CompactMessageCodec()
    original = SessionCompactInput(request.trigger, codec.encode(request.messages), instructions=request.instructions)
    frame = _CompactFrame(original, request, context, continuation, codec, asyncio.current_task())
    token = _FRAME.set(frame)
    try:
        result = await bus.trigger(Event(SESSION_COMPACT_EVENT, original), context)
        if not valid_compact_output(result):
            raise CompactionError("COMPACT_INVALID_RESULT")
        if isinstance(result, SessionCompactSkipped):
            return CompactionSkipped(result.skip)
        return CompactionCandidate(codec.decode(result.messages))
    finally:
        frame.active = False
        codec.handles.clear()
        _FRAME.reset(token)
