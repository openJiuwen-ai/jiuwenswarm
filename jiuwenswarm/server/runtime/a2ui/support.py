# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility facade for the modular A2UI feature package.

New code should import from the focused modules directly. This facade exposes
the remaining A2UI protocol and formatting helpers for older internal tests.
"""

from jiuwenswarm.server.runtime.a2ui.prompt_instructions import build_a2ui_autonomy_instruction
from jiuwenswarm.server.runtime.a2ui.protocol import (
    A2UI_ACTIVE_PROTOCOL_VERSION,
    A2UI_CLIENT_EVENT_TYPE,
    A2UI_CLOSE_TAG,
    A2UI_OPEN_TAG,
    A2UIProtocolSpec,
    A2UIStreamGuard,
    build_a2ui_client_event_prompt,
    build_a2ui_prompt_section,
    format_a2ui_for_text_channel,
    format_content_for_channel,
    get_protocol_spec,
    is_a2ui_client_event,
)
from jiuwenswarm.server.runtime.a2ui.types import (
    A2UIExample,
    A2UIResponsePart,
    A2UIValidationResult,
)


__all__ = [
    "A2UI_ACTIVE_PROTOCOL_VERSION",
    "A2UI_CLIENT_EVENT_TYPE",
    "A2UI_CLOSE_TAG",
    "A2UI_OPEN_TAG",
    "A2UIExample",
    "A2UIProtocolSpec",
    "A2UIResponsePart",
    "A2UIStreamGuard",
    "A2UIValidationResult",
    "build_a2ui_autonomy_instruction",
    "build_a2ui_client_event_prompt",
    "build_a2ui_prompt_section",
    "format_a2ui_for_text_channel",
    "format_content_for_channel",
    "get_protocol_spec",
    "is_a2ui_client_event",
]
