# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adaptive Card rendering for Microsoft Teams (E7 - step 1).

Teams is the enterprise gap: no adapter exists today. Its buttons come
from Adaptive Cards - a JSON document attached to the activity - and a
click arrives back as an activity whose ``value`` carries whatever the
action put there.

Pure helpers over dictionaries, so the rendering is unit-tested without
the Bot Framework SDK: the adapter wraps the returned card in an
attachment and posts it.
"""
from __future__ import annotations

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
ADAPTIVE_CARD_VERSION = "1.4"
ACTION_KEY = "jiuwen_action"


def render_card(card_spec: dict) -> dict:
    """Translate an ``interactive_card`` payload into an Adaptive Card."""
    actions = [
        {
            "type": "Action.Submit",
            "title": str(button.get("label", "")),
            "data": {ACTION_KEY: str(button.get("action", ""))},
        }
        for button in card_spec.get("buttons", ())
        if button.get("label")
    ]
    card: dict = {
        "$schema": ADAPTIVE_CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "body": [
            {
                "type": "TextBlock",
                "text": str(card_spec.get("text", "")),
                "wrap": True,
            }
        ],
    }
    if actions:
        card["actions"] = actions
    return card


def card_attachment(card_spec: dict) -> dict:
    """Wrap the card in the attachment shape a Teams activity expects."""
    return {
        "contentType": ADAPTIVE_CARD_CONTENT_TYPE,
        "content": render_card(card_spec),
    }


def extract_action(activity_value: object) -> str | None:
    """Read the card action back from a submitted activity value."""
    if not isinstance(activity_value, dict):
        return None
    action = activity_value.get(ACTION_KEY)
    if action is None:
        return None
    action = str(action).strip()
    return action or None


def strip_mention(text: str, bot_name: str) -> str:
    """Remove the ``<at>Bot</at>`` mention Teams prepends in channels."""
    cleaned = str(text or "")
    if bot_name:
        for form in (f"<at>{bot_name}</at>", f"@{bot_name}"):
            cleaned = cleaned.replace(form, " ")
    cleaned = cleaned.replace("<at>", " ").replace("</at>", " ")
    return " ".join(cleaned.split())


def conversation_key(tenant_id: str, conversation_id: str) -> str:
    """Session key for a Teams conversation: one per tenant and thread."""
    return f"{str(tenant_id or '').strip()}|{str(conversation_id or '').strip()}"