# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.gateway.channel_manager.sdk import (
    Button,
    InteractiveCard,
    RichText,
    Span,
    SpanStyle,
    render,
)


# ---------------------------------------------------------------- rich text
def test_render_markdown_translates_styles():
    rich = RichText.of(
        Span("Deploy "), Span("v2.3", SpanStyle.BOLD), Span(" now "),
        Span("make deploy", SpanStyle.CODE),
    )
    assert render(rich, "markdown", rich_text_supported=True) == "Deploy *v2.3* now `make deploy`"


def test_render_degrades_to_plain_when_unsupported():
    rich = RichText.of(Span("Deploy "), Span("v2.3", SpanStyle.BOLD))
    assert render(rich, "markdown", rich_text_supported=False) == "Deploy v2.3"


def test_render_unknown_dialect_falls_back_to_plain():
    rich = RichText.of(Span("hello ", SpanStyle.CODE), Span("world"))
    assert render(rich, "no-such-dialect", rich_text_supported=True) == "hello world"


# -------------------------------------------------------------------- cards
def test_card_degrades_to_numbered_reply_text():
    card = InteractiveCard.of(
        "Deploy v2.3?", Button("Approve", "approve"), Button("Deny", "deny"),
    )
    degraded = card.degrade_to_text()
    assert degraded.startswith("Deploy v2.3?")
    assert "Reply 1: Approve" in degraded and "Reply 2: Deny" in degraded


def test_card_maps_numbered_reply_back_to_action():
    card = InteractiveCard.of(
        "Deploy v2.3?", Button("Approve", "approve"), Button("Deny", "deny"),
    )
    assert card.action_for_reply("1") == "approve"
    assert card.action_for_reply(" 2 ") == "deny"
    assert card.action_for_reply("7") is None
    assert card.action_for_reply("yes") is None


def test_card_without_buttons_degrades_to_its_text_only():
    card = InteractiveCard.of("Plain announcement")
    assert card.degrade_to_text() == "Plain announcement"
    assert card.action_for_reply("1") is None