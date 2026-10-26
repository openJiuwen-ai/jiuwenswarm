# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime helpers for A2UI request handling."""

from jiuwenswarm.server.runtime.a2ui.protocol import (
    A2UIStreamGuard,
    build_a2ui_client_event_prompt,
    build_a2ui_prompt_section,
    format_a2ui_for_text_channel,
    format_content_for_channel,
    is_a2ui_client_event,
)
from jiuwenswarm.server.runtime.a2ui.runtime.response_finalization import finalize_a2ui_assistant_content

__all__ = [
    "A2UIStreamGuard",
    "build_a2ui_client_event_prompt",
    "build_a2ui_prompt_section",
    "finalize_a2ui_assistant_content",
    "format_a2ui_for_text_channel",
    "format_content_for_channel",
    "is_a2ui_client_event",
]
