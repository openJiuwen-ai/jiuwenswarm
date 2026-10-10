"""Capability-aware outgoing resolution (E2 - step 1 of 3).

Purely additive: nothing existing imports this yet. The send path will
be wired in the next PR. Rule: NEVER return None - every content maps
to something deliverable.
"""
from dataclasses import dataclass

from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
from jiuwenswarm.gateway.channel_manager.sdk.cards import InteractiveCard


@dataclass(frozen=True)
class OutgoingMessage:
    """Resolved outgoing content: either a native card or plain text."""
    kind: str            # "card" | "text"
    payload: object      # InteractiveCard | str
    degraded: bool = False


def resolve_outgoing(content, capabilities: ChannelCapabilities) -> OutgoingMessage:
    """Adapt *content* to what the channel can render.

    - InteractiveCard + buttons supported  -> native card
    - InteractiveCard + no buttons         -> numbered-reply text (degraded)
    - anything else                        -> plain text passthrough
    """
    if isinstance(content, InteractiveCard):
        if capabilities.buttons:
            return OutgoingMessage(kind="card", payload=content)
        return OutgoingMessage(
            kind="text", payload=content.degrade_to_text(), degraded=True
        )
    return OutgoingMessage(kind="text", payload=str(content))