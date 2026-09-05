# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wiring tests for capability-aware dispatch (E2 step 3)."""
from __future__ import annotations

import dataclasses

from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
from jiuwenswarm.gateway.message_handler.delivery import (
    PendingCardRegistry,
    degrade_card_if_unsupported,
    map_incoming_reply,
)


@dataclasses.dataclass
class FakeMessage:
    session_id: str
    params: dict
    payload: dict


class FakeChannel:
    def __init__(self, **caps):
        self.capabilities = ChannelCapabilities(**caps)


CARD_SPEC = {
    "text": "Deploy v2.3?",
    "buttons": ({"label": "Approve", "action": "approve"},
                {"label": "Deny", "action": "deny"}),
}


def test_card_degraded_on_poor_channel_and_remembered():
    registry = PendingCardRegistry()
    msg = FakeMessage("web:chaimae", {}, {"interactive_card": CARD_SPEC})
    out = degrade_card_if_unsupported(msg, FakeChannel(buttons=False), registry)
    assert "Reply 1: Approve" in out.params["content"]
    assert "interactive_card" not in out.payload
    assert registry.resolve_incoming_reply("web:chaimae", "1") == ("action", "approve")


def test_card_untouched_on_rich_channel():
    registry = PendingCardRegistry()
    msg = FakeMessage("s", {}, {"interactive_card": CARD_SPEC})
    out = degrade_card_if_unsupported(msg, FakeChannel(buttons=True), registry)
    assert out is msg
    assert registry.resolve_incoming_reply("s", "1") == ("text", "1")


def test_non_card_message_untouched():
    registry = PendingCardRegistry()
    msg = FakeMessage("s", {"content": "hello"}, {})
    assert degrade_card_if_unsupported(msg, FakeChannel(), registry) is msg


def test_channel_without_capabilities_attr_defaults_conservative():
    registry = PendingCardRegistry()

    class BareChannel:
        pass

    msg = FakeMessage("s", {}, {"interactive_card": CARD_SPEC})
    out = degrade_card_if_unsupported(msg, BareChannel(), registry)
    assert "Reply 1" in out.params["content"]


def test_incoming_numbered_reply_rewritten_to_action():
    registry = PendingCardRegistry()
    sent = FakeMessage("web:chaimae", {}, {"interactive_card": CARD_SPEC})
    degrade_card_if_unsupported(sent, FakeChannel(buttons=False), registry)
    incoming = FakeMessage("web:chaimae", {"content": "1"}, {})
    out = map_incoming_reply(incoming, registry)
    assert out.params["content"] == "approve"


def test_incoming_plain_text_flows_unchanged():
    registry = PendingCardRegistry()
    incoming = FakeMessage("s", {"content": "bonjour"}, {})
    assert map_incoming_reply(incoming, registry) is incoming