# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-owned send redaction and receive consumption on the real mailbox."""

from dataclasses import replace
import logging

from openjiuwen.core.events import Event

from jiuwenswarm.extensions_v2.event_types.session_receive import SessionReceiveConsumed, SessionReceiveResult
from jiuwenswarm.extensions_v2.event_types.session_send import SessionSendDenied

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_delivery_policy")


class SessionDeliveryPolicy:
    """Policies edit text without selecting another source, target or user."""

    def register(self, registrar):
        registrar.register_event_handler("session.send", self.on_send, {}, "user")
        registrar.register_event_handler(
            "session.receive", self.on_receive, {"origin.kind": "peer-send-message"}, "user",
        )

    async def on_send(self, event, ops, next_handler):
        value = event.event_input
        if value.text.startswith("[manual]"):
            return SessionSendDenied(deny="This request requires a manual send.")
        text = value.text.replace("13800138000", "[phone]")
        logger.info("send policy: source=%s target=%s", ops.session_context.session_id, value.target_session_id)
        return await next_handler(Event(event.event_type, replace(value, text=text)))

    async def on_receive(self, event, ops, next_handler):
        if event.event_input.text.startswith("[ignore]"):
            return SessionReceiveConsumed(consumed="The plugin handled this notification.")
        receipt = await next_handler(event)
        if isinstance(receipt, SessionReceiveResult):
            logger.info("receive policy: target=%s accepted_chars=%s", ops.session_context.session_id, len(receipt.text))
        return receipt


def create_extension():
    return SessionDeliveryPolicy()
