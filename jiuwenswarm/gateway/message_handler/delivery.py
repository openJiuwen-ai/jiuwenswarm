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

_default_registry = PendingCardRegistry()


def get_default_registry() -> PendingCardRegistry:
    """Shared registry used by the dispatch loop and the inbound path."""
    return _default_registry


def degrade_card_if_unsupported(msg, channel, registry):
    """Rewrite an interactive-card message for channels without buttons.

    No-op when the message carries no card or the channel declares button
    support. Otherwise the card is remembered for the session and the
    message content becomes its numbered-reply text - never dropped.
    """
    import dataclasses

    from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
    from jiuwenswarm.gateway.channel_manager.sdk.cards import Button

    payload = getattr(msg, "payload", None) or {}
    card_spec = payload.get("interactive_card")
    if not card_spec:
        return msg
    capabilities = getattr(channel, "capabilities", None) or ChannelCapabilities()
    if capabilities.buttons:
        return msg
    card = InteractiveCard(
        text=card_spec.get("text", ""),
        buttons=tuple(
            Button(label=b["label"], action=b["action"])
            for b in card_spec.get("buttons", ())
        ),
    )
    registry.remember(msg.session_id or "", card)
    new_payload = {k: v for k, v in payload.items() if k != "interactive_card"}
    new_params = dict(getattr(msg, "params", None) or {})
    new_params["content"] = card.degrade_to_text()
    return dataclasses.replace(msg, payload=new_payload, params=new_params)


def map_incoming_reply(msg, registry):
    """Rewrite a numbered reply back into its card action on the way in."""
    import dataclasses

    params = dict(getattr(msg, "params", None) or {})
    content = str(params.get("content", ""))
    kind, value = registry.resolve_incoming_reply(msg.session_id or "", content)
    if kind == "action":
        params["content"] = value
        return dataclasses.replace(msg, params=params)
    return msg