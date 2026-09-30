# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure Block Kit rendering for interactive cards (E3 - step 1).

Standalone helpers so the rendering is testable without the Slack SDK.
"""
from __future__ import annotations

CARD_ACTION_ID = "jiuwen_card_action"


def render_card_blocks(card_spec: dict) -> list[dict]:
    """Translate an ``interactive_card`` payload into Slack Block Kit blocks."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": card_spec.get("text", "")},
        }
    ]
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": button["label"]},
            "action_id": f"{CARD_ACTION_ID}_{index}",
            "value": button["action"],
        }
        for index, button in enumerate(card_spec.get("buttons", ()))
    ]
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    return blocks


def extract_action_value(action_body: dict) -> str | None:
    """Pull the card action value out of a Slack block_actions payload."""
    for action in action_body.get("actions", ()):
        if str(action.get("action_id", "")).startswith(CARD_ACTION_ID):
            value = action.get("value")
            if value:
                return str(value)
    return None