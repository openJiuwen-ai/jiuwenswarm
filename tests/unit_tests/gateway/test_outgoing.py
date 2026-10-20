"""Tests for capability-aware outgoing resolution (E2 step 1)."""
import pytest

from jiuwenswarm.gateway.channel_manager.sdk.capabilities import ChannelCapabilities
from jiuwenswarm.gateway.channel_manager.sdk.cards import Button, InteractiveCard
from jiuwenswarm.gateway.message_handler.outgoing import OutgoingMessage, resolve_outgoing


APPROVAL = InteractiveCard(
    text="Deploy v2.3?",
    buttons=(Button("Approve", "approve"), Button("Deny", "deny")),
)


def test_card_on_button_channel_stays_native():
    out = resolve_outgoing(APPROVAL, ChannelCapabilities(buttons=True))
    assert out.kind == "card" and out.payload is APPROVAL and not out.degraded


def test_card_on_textonly_channel_degrades_to_numbered_replies():
    out = resolve_outgoing(APPROVAL, ChannelCapabilities(buttons=False))
    assert out.kind == "text" and out.degraded
    assert "Reply 1: Approve" in out.payload and "Reply 2: Deny" in out.payload


def test_default_capability_sheet_is_conservative():
    assert resolve_outgoing(APPROVAL, ChannelCapabilities()).degraded


def test_plain_text_is_passthrough_on_any_channel():
    for caps in (ChannelCapabilities(), ChannelCapabilities(buttons=True)):
        assert resolve_outgoing("hello", caps) == OutgoingMessage("text", "hello")


def test_resolution_never_returns_none():
    for content in (APPROVAL, "hi", 42, None):
        assert resolve_outgoing(content, ChannelCapabilities()) is not None


def test_degraded_text_preserves_original_question():
    out = resolve_outgoing(APPROVAL, ChannelCapabilities())
    assert "Deploy v2.3?" in out.payload


def test_result_is_immutable_like_the_rest_of_the_sdk():
    out = resolve_outgoing("x", ChannelCapabilities())
    with pytest.raises(Exception):
        out.kind = "card"