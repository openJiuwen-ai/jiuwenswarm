# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Heartbeat push routing: keep the request route, skip IM runtime telemetry."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.heartbeat_push import (
    is_heartbeat_im_channel,
    merge_heartbeat_push_metadata,
    should_skip_heartbeat_push,
)

_ROUTE = {
    "app_id": "cli_app_a",
    "bot_id": "cli_app_a",
    "provider": "feishu",
    "chat_id": "oc_chat",
    "user_id": "ou_user",
    "automation": {"kind": "heartbeat", "run_id": "run-1"},
}


def test_event_metadata_keeps_request_route_identity() -> None:
    """An event must not redirect a heartbeat to another bot, chat, or user."""
    merged = merge_heartbeat_push_metadata(
        _ROUTE,
        {"model": "m1", "app_id": "wrong_app", "chat_id": ""},
    )

    assert merged["app_id"] == "cli_app_a"
    assert merged["bot_id"] == "cli_app_a"
    assert merged["chat_id"] == "oc_chat"
    assert merged["user_id"] == "ou_user"
    assert merged["provider"] == "feishu"
    assert merged["automation"]["kind"] == "heartbeat"
    # Event-owned telemetry survives the merge.
    assert merged["model"] == "m1"


@pytest.mark.parametrize("event_metadata", [None, {}, "not-a-dict"])
def test_route_survives_missing_or_invalid_event_metadata(event_metadata) -> None:
    merged = merge_heartbeat_push_metadata(_ROUTE, event_metadata)

    assert merged["app_id"] == "cli_app_a"
    assert merged["chat_id"] == "oc_chat"


def test_route_omitted_values_never_blank_an_event_key() -> None:
    merged = merge_heartbeat_push_metadata(
        {"app_id": "", "bot_id": None},
        {"app_id": "event_app", "bot_id": "event_bot"},
    )

    assert merged["app_id"] == "event_app"
    assert merged["bot_id"] == "event_bot"


@pytest.mark.parametrize(
    "channel_id",
    [
        "web",
        "web:default",
        "tui",
        "TUI",
        # Heartbeats are only excluded for xiaoyi, so every UI channel below can
        # own a heartbeat job and must keep its thinking/usage telemetry.
        "browser",
        "browser:default",
        "ide",
        "ide:vscode",
        "desktop",
        "process_cli",
        "cli",
        "acp",
    ],
)
def test_ui_channels_consume_runtime_telemetry(channel_id: str) -> None:
    assert is_heartbeat_im_channel(channel_id) is False
    assert should_skip_heartbeat_push("chat.llm_call_end", channel_id) is False


def test_unknown_channels_keep_runtime_telemetry() -> None:
    """An unlisted channel is a UI, not an IM: losing telemetry is a regression."""
    assert is_heartbeat_im_channel("future_ui_channel") is False
    assert is_heartbeat_im_channel("") is False
    assert is_heartbeat_im_channel(None) is False
    assert should_skip_heartbeat_push("chat.llm_call_end", "future_ui_channel") is False


@pytest.mark.parametrize(
    "channel_id",
    [
        "feishu",
        "feishu:cli_app_a",
        "feishu_enterprise:cli_app_a",
        "dingtalk",
        "welink",
        "wecom",
        "wechat",
        "slack",
        "telegram",
        "discord",
        "whatsapp",
        "xiaoyi",
        "im",
        "FEISHU:cli_app_a",
    ],
)
def test_im_channels_skip_runtime_telemetry(channel_id: str) -> None:
    assert is_heartbeat_im_channel(channel_id) is True
    assert should_skip_heartbeat_push("chat.llm_call_end", channel_id) is True
    assert should_skip_heartbeat_push("chat.usage_metadata", channel_id) is True
    assert should_skip_heartbeat_push("context.usage", channel_id) is True


@pytest.mark.parametrize(
    "event_type",
    [
        "chat.delta",
        "chat.final",
        "chat.error",
        "chat.interrupt_result",
        "chat.processing_status",
        "chat.tool_call",
        "chat.tool_result",
    ],
)
def test_im_channels_keep_user_visible_events(event_type: str) -> None:
    assert should_skip_heartbeat_push(event_type, "feishu") is False


def test_normalized_terminal_errors_are_never_skipped() -> None:
    """Heartbeat normalizes execution.error to chat.error before this check."""
    assert should_skip_heartbeat_push("chat.error", "feishu") is False
