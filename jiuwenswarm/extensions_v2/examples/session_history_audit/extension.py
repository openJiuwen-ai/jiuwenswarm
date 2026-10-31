# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Append and read an audited note without sending another Agent request."""

from dataclasses import replace
import hashlib
import logging

from openjiuwen.core.events import Event

from jiuwenswarm.extensions_v2.event_types.session_history import SessionAppendRejection

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_history_audit")


class SessionHistoryAudit:
    """User-owned policy and history operations using host capabilities."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.append", self.on_append, {"door": "note", "origin.kind": "extension"}, "user")

    async def on_usage(self, event, ops, next_handler):
        message_id = "audit-" + hashlib.sha256(event.event_input.request_id.encode("utf-8")).hexdigest()
        receipt = await ops.session.append({"id": message_id, "message": {
            "type": "assistant", "content": [{"type": "text", "text": "This turn was audited."}],
        }})
        messages = await ops.session.messages()
        saved_text = "".join(block["text"] for block in receipt.get("message", {}).get("content", [])
                             if block.get("type") == "text") if receipt.get("message") else None
        visible = saved_text is not None and any(row["text"] == saved_text for row in messages[-5:])
        logger.info("session history audit: session=%s status=%s visible=%s", ops.session_context.session_id, receipt["status"], visible)
        return await next_handler(event)

    async def on_append(self, event, ops, next_handler):
        message = event.event_input.message
        text = "".join(block["text"] for block in message.content if block.get("type") == "text")
        if text.startswith("[block]"):
            return SessionAppendRejection("The audit policy rejected this note.")
        blocks = [{**block, "text": "[audited] " + block["text"]} if block.get("type") == "text" else block for block in message.content]
        rewritten = replace(event.event_input, message=replace(message, content=blocks))
        return await next_handler(Event(event.event_type, rewritten))


def create_extension():
    return SessionHistoryAudit()
