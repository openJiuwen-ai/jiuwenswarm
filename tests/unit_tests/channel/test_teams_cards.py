# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Teams Adaptive Card rendering (E7 step 1)."""
from jiuwenswarm.gateway.channel_manager.im_platforms.teams.teams_cards import (
    ACTION_KEY,
    ADAPTIVE_CARD_CONTENT_TYPE,
    ADAPTIVE_CARD_VERSION,
    card_attachment,
    conversation_key,
    extract_action,
    render_card,
    strip_mention,
)