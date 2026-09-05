# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Capability-aware delivery (E2 - step 2 of 3).

Builds on ``outgoing.resolve_outgoing`` (E2 step 1): ``deliver()`` is the
single entry point the send path will call, and ``PendingCardRegistry``
remembers degraded cards per session so numbered replies can be mapped
back to card actions (step 3 wires both into ``message_handler.py``).

Postcondition of ``deliver()``: the channel ALWAYS receives exactly one
message - never a silent drop.
"""
from __future__ import annotations

import logging

from jiuwenswarm.gateway.channel_manager.sdk.cards import InteractiveCard
from jiuwenswarm.gateway.message_handler.outgoing import (
    OutgoingMessage,
    resolve_outgoing,
)

logger = logging.getLogger(__name__)


class PendingCardRegistry:
    """Degraded cards awaiting a numbered reply, keyed by session id."""

    def __init__(self) -> None:
        self._pending: dict[str, InteractiveCard] = {}

    def remember(self, session_id: str, card: InteractiveCard) -> None:
        self._pending[session_id] = card

    def resolve_incoming_reply(self, session_id: str, text: str) -> tuple[str, str]:
        """Map a numbered reply back to its card action.

        Returns ``("action", <action>)`` when *text* is a valid numbered
        reply for the session's pending card (the card is then consumed),
        else ``("text", text)`` unchanged - incoming content is never
        dropped either.
        """
        card = self._pending.get(session_id)
        if card is not None:
            action = card.action_for_reply(text)
            if action is not None:
                del self._pending[session_id]
                return ("action", action)
        return ("text", text)


async def deliver(channel, content, session_id: str,
                  registry: PendingCardRegistry) -> OutgoingMessage:
    """Resolve *content* against the channel capabilities and send it.

    Replaces the silent-drop branch: every content maps to exactly one
    outgoing message. Degraded cards are remembered so the reply loop
    can translate "1"/"2" back into actions.
    """
    resolved = resolve_outgoing(content, channel.capabilities)
    if resolved.kind == "card":
        await channel.send_card(resolved.payload)
    else:
        await channel.send_text(resolved.payload)
        if resolved.degraded:
            registry.remember(session_id, content)
            logger.info(
                "card degraded to numbered-reply text for session %s", session_id
            )
    return resolved