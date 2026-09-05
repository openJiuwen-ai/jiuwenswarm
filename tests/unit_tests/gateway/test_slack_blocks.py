# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Block Kit card rendering (E3 step 1)."""
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_blocks import (
    CARD_ACTION_ID,
    extract_action_value,
    render_card_blocks,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import SlackChannel

CARD = {
    "text": "Deploy v2.3?",
    "buttons": ({"label": "Approve", "action": "approve"},
                {"label": "Deny", "action": "deny"}),
}


def test_card_renders_section_plus_actions():
    blocks = render_card_blocks(CARD)
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == "Deploy v2.3?"
    assert blocks[1]["type"] == "actions"
    assert [b["text"]["text"] for b in blocks[1]["elements"]] == ["Approve", "Deny"]
    assert [b["value"] for b in blocks[1]["elements"]] == ["approve", "deny"]


def test_action_ids_are_unique_and_prefixed():
    blocks = render_card_blocks(CARD)
    ids = [b["action_id"] for b in blocks[1]["elements"]]
    assert len(set(ids)) == 2
    assert all(i.startswith(CARD_ACTION_ID) for i in ids)


def test_card_without_buttons_renders_section_only():
    assert len(render_card_blocks({"text": "hello"})) == 1


def test_extract_action_value_from_block_actions_payload():
    body = {"actions": [{"action_id": f"{CARD_ACTION_ID}_0", "value": "approve"}]}
    assert extract_action_value(body) == "approve"


def test_extract_ignores_foreign_actions():
    body = {"actions": [{"action_id": "other_widget", "value": "x"}]}
    assert extract_action_value(body) is None


def test_slack_declares_buttons_capability():
    assert SlackChannel.capabilities.buttons is True
