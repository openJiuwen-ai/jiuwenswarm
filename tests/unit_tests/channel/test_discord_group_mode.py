# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Discord group chat modes (E4 step 1)."""
from jiuwenswarm.gateway.channel_manager.im_platforms.discord.discord_group_mode import (
    GROUP_MODE_MENTION,
    mentions_bot,
    normalize_group_chat_mode,
    should_handle,
    strip_bot_mention,
)

BOT = "999"
MENTION_TEXT = "<@999> summarize this"
PLAIN_TEXT = "hey team, lunch at noon?"


def test_unknown_mode_falls_back_to_mention():
    assert normalize_group_chat_mode("nonsense") == GROUP_MODE_MENTION
    assert normalize_group_chat_mode(None) == GROUP_MODE_MENTION
    assert normalize_group_chat_mode(" ALL ") == "all"


def test_direct_messages_are_never_filtered():
    for mode in ("all", "mention", "reply", "off"):
        assert should_handle(PLAIN_TEXT, mode=mode, bot_user_id=BOT,
                             is_direct_message=True) is True


def test_mention_mode_answers_only_when_mentioned():
    assert should_handle(MENTION_TEXT, mode="mention", bot_user_id=BOT,
                         is_direct_message=False) is True
    assert should_handle(PLAIN_TEXT, mode="mention", bot_user_id=BOT,
                         is_direct_message=False) is False


def test_all_mode_answers_every_channel_message():
    assert should_handle(PLAIN_TEXT, mode="all", bot_user_id=BOT,
                         is_direct_message=False) is True


def test_off_mode_answers_nothing_in_channels():
    assert should_handle(MENTION_TEXT, mode="off", bot_user_id=BOT,
                         is_direct_message=False) is False


def test_reply_mode_covers_replies_and_mentions():
    assert should_handle(PLAIN_TEXT, mode="reply", bot_user_id=BOT,
                         is_direct_message=False, is_reply_to_bot=True) is True
    assert should_handle(MENTION_TEXT, mode="reply", bot_user_id=BOT,
                         is_direct_message=False) is True
    assert should_handle(PLAIN_TEXT, mode="reply", bot_user_id=BOT,
                         is_direct_message=False) is False


def test_nickname_mention_form_is_recognized():
    assert mentions_bot("<@!999> hello", BOT) is True


def test_another_users_mention_is_not_the_bot():
    assert mentions_bot("<@123> hello", BOT) is False


def test_strip_removes_mention_and_collapses_spaces():
    assert strip_bot_mention(MENTION_TEXT, BOT) == "summarize this"
    assert strip_bot_mention("<@!999>   ping   now", BOT) == "ping now"


def test_strip_without_bot_id_returns_trimmed_text():
    assert strip_bot_mention("  hello  ", "") == "hello"


def test_missing_bot_id_never_matches_a_mention():
    assert should_handle(MENTION_TEXT, mode="mention", bot_user_id="",
                         is_direct_message=False) is False
                         