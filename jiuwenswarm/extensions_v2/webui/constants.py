# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI extension constants: events, slots and size limits."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class WebUIEvent(StrEnum):
    """Event names shared by the extension bus and the browser bridge."""

    RENDER = "ui.render"
    READY = "ui.ready"
    SELECT = "ui.select"
    CLICK = "ui.click"
    CHANGE = "ui.change"
    PRESS_ENTER = "ui.press_enter"
    SEARCH = "ui.search"
    CLEAR = "ui.clear"


# Downstream frames consumed by the browser.
FRAME_RENDER: Final[str] = "webui.render"
FRAME_CLEAR: Final[str] = "webui.render.clear"
FRAME_RENDER_ERROR: Final[str] = "webui.render.error"

# Upstream events: all enter the event chain through the single method webui.dispatch.
UPSTREAM_EVENTS: Final[frozenset[str]] = frozenset(
    {
        WebUIEvent.READY.value,
        WebUIEvent.SELECT.value,
        WebUIEvent.CLICK.value,
        WebUIEvent.CHANGE.value,
        WebUIEvent.PRESS_ENTER.value,
        WebUIEvent.SEARCH.value,
        WebUIEvent.CLEAR.value,
    }
)

# Slot mount acknowledgement: the instance does not exist yet, so Liveness is not checked.
SLOT_READY_EVENT: Final[str] = WebUIEvent.READY.value

RENDERER_WEBUI_TREE: Final[str] = "webui-tree"
RENDER_SCHEMA_VERSION: Final[int] = 1
DEFAULT_CHANNEL_ID: Final[str] = "web"

# Only conversation-scoped slots are served; downstream frames carry session_id for routing.
SESSION_SLOTS: Final[Mapping[str, str]] = {
    "conversation.input.above": "list",
    "conversation.input.below": "single",
    "conversation.panel.right": "list",
    "application.menu": "list",
    "application.menu.main": "single",
    "task.function": "list",
    "task.function.main": "single",
    "application.settings": "list",
    "conversation.turn.content.after": "list",
    "conversation.turn.footer.actions": "list",
    "conversation.header.title.after": "list",
    "conversation.header.actions": "list",
    "message.assistant.meta": "list",
}

# Size limits: the first four match the existing wire truncation budget.
MAX_DEPTH: Final[int] = 8
MAX_PROP_DEPTH: Final[int] = MAX_DEPTH * 3
MAX_LIST_ITEMS: Final[int] = 100
MAX_TEXT_BYTES: Final[int] = 16 * 1024
MAX_NODES: Final[int] = 512
MAX_FRAME_BYTES: Final[int] = 256 * 1024
MAX_INSTANCES_PER_SESSION: Final[int] = 32
MAX_ACTION_ARGS_BYTES: Final[int] = 8 * 1024

# Error codes shared by downstream error frames and upstream receipts.
CODE_SCHEMA_ERROR: Final[str] = "SCHEMA_ERROR"
CODE_TARGET_INACTIVE: Final[str] = "TARGET_INACTIVE"
CODE_DELIVERY_FAILED: Final[str] = "DELIVERY_FAILED"
CODE_SESSION_MISMATCH: Final[str] = "SESSION_MISMATCH"
CODE_WEBUI_DISABLED: Final[str] = "WEBUI_UNAVAILABLE"
CODE_NO_CONTRIBUTION: Final[str] = "NO_CONTRIBUTION"
