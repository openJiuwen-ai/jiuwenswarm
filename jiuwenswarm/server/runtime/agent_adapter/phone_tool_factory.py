# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load handset tools only when the runtime enables the phone capability."""

from __future__ import annotations

from typing import Any


# Preserve the established registration order, independently of package exports.
_PHONE_TOOL_NAMES = (
    "get_user_location",
    "create_note",
    "search_notes",
    "modify_note",
    "create_calendar_event",
    "search_calendar_event",
    "search_contact",
    "search_photo_gallery",
    "upload_photo",
    "search_file",
    "upload_file",
    "call_phone",
    "send_message",
    "search_message",
    "create_alarm",
    "search_alarms",
    "modify_alarm",
    "delete_alarm",
    "query_collection",
    "add_collection",
    "delete_collection",
    "save_media_to_gallery",
    "save_file_to_file_manager",
    "convert_timestamp_to_utc8_time",
    "view_push_result",
    "image_reading",
    "xiaoyi_gui_agent",
)


def get_xiaoyi_phone_tools() -> list[Any]:
    """Return the original decorated tools, without wrapping or rebuilding them."""
    from jiuwenswarm.agents.harness.common.tools import xiaoyi_phone_tools

    return [getattr(xiaoyi_phone_tools, name) for name in _PHONE_TOOL_NAMES]
