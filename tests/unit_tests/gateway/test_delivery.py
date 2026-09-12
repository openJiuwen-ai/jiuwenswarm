# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mock-channel tests for capability-aware delivery (E2 step 2)."""
import pytest

from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
from jiuwenswarm.gateway.channel_manager.sdk.cards import Button, InteractiveCard
from jiuwenswarm.gateway.message_handler.delivery import PendingCardRegistry, deliver


class MockChannel:
    def __init__(self, **caps):
        self.capabilities = ChannelCapabilities(**caps)
        self.sent_cards = []
        self.sent_texts = []

    async def send_card(self, card):
        self.sent_cards.append(card)

    async def send_text(self, text):
        self.sent_texts.append(text)


APPROVAL = InteractiveCard(
    text="Deploy v2.3?",
    buttons=(Button("Approve", "approve"), Button("Deny", "deny")),
)


@pytest.mark.asyncio
async def test_rich_channel_receives_native_card():
    channel, registry = MockChannel(buttons=True), PendingCardRegistry()
    await deliver(channel, APPROVAL, "web:chaimae", registry)
    assert channel.sent_cards == [APPROVAL]
    assert channel.sent_texts == []


@pytest.mark.asyncio
async def test_poor_channel_receives_degraded_text_not_nothing():
    channel, registry = MockChannel(buttons=False), PendingCardRegistry()
    await deliver(channel, APPROVAL, "web:chaimae", registry)
    assert channel.sent_cards == []
    assert len(channel.sent_texts) == 1
    assert "Reply 1: Approve" in channel.sent_texts[0]


@pytest.mark.asyncio
async def test_every_channel_receives_exactly_one_message():
    registry = PendingCardRegistry()
    for caps in ({}, {"buttons": True}, {"buttons": False, "streaming": True}):
        channel = MockChannel(**caps)
        await deliver(channel, APPROVAL, "s", registry)
        assert len(channel.sent_cards) + len(channel.sent_texts) == 1


@pytest.mark.asyncio
async def test_plain_text_is_delivered_unchanged():
    channel, registry = MockChannel(), PendingCardRegistry()
    await deliver(channel, "hello", "s", registry)
    assert channel.sent_texts == ["hello"]


@pytest.mark.asyncio
async def test_numbered_reply_maps_back_to_action():
    channel, registry = MockChannel(buttons=False), PendingCardRegistry()
    await deliver(channel, APPROVAL, "web:chaimae", registry)
    assert registry.resolve_incoming_reply("web:chaimae", "1") == ("action", "approve")


@pytest.mark.asyncio
async def test_reply_2_maps_to_deny():
    channel, registry = MockChannel(buttons=False), PendingCardRegistry()
    await deliver(channel, APPROVAL, "web:chaimae", registry)
    assert registry.resolve_incoming_reply("web:chaimae", "2") == ("action", "deny")


def test_reply_without_pending_card_is_plain_text():
    registry = PendingCardRegistry()
    assert registry.resolve_incoming_reply("s", "1") == ("text", "1")


@pytest.mark.asyncio
async def test_invalid_reply_is_forwarded_not_dropped():
    channel, registry = MockChannel(buttons=False), PendingCardRegistry()
    await deliver(channel, APPROVAL, "s", registry)
    assert registry.resolve_incoming_reply("s", "yes please") == ("text", "yes please")


@pytest.mark.asyncio
async def test_pending_card_is_consumed_after_valid_reply():
    channel, registry = MockChannel(buttons=False), PendingCardRegistry()
    await deliver(channel, APPROVAL, "s", registry)
    registry.resolve_incoming_reply("s", "1")
    assert registry.resolve_incoming_reply("s", "1") == ("text", "1")


@pytest.mark.asyncio
async def test_no_cross_session_leakage():
    channel, registry = MockChannel(buttons=False), PendingCardRegistry()
    await deliver(channel, APPROVAL, "session-a", registry)
    assert registry.resolve_incoming_reply("session-b", "1") == ("text", "1")


@pytest.mark.asyncio
async def test_native_card_leaves_nothing_pending():
    channel, registry = MockChannel(buttons=True), PendingCardRegistry()
    await deliver(channel, APPROVAL, "s", registry)
    assert registry.resolve_incoming_reply("s", "1") == ("text", "1")