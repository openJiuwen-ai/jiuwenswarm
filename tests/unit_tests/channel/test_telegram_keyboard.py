# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Telegram inline keyboard rendering (E4)."""
from jiuwenswarm.gateway.channel_manager.im_platforms.telegram.telegram_keyboard import (
    BUTTONS_PER_ROW,
    CALLBACK_LIMIT,
    CALLBACK_PREFIX,
    callback_data,
    card_text,
    extract_action,
    render_keyboard,
)