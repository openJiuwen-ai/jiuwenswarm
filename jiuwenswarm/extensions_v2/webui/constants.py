# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI extension constants: events, slots and size limits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
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

class SlotScope(StrEnum):
    """Whether a slot is tied to one chat or to one browser connection."""

    SESSION = "session"
    APPLICATION = "application"


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """One allowlisted slot. New positions are new rows, not new branches."""

    capacity: str
    scope: SlotScope


def _slot(capacity: str, scope: SlotScope = SlotScope.SESSION) -> SlotSpec:
    return SlotSpec(capacity=capacity, scope=scope)


# Allowlist. Session rows still require a chat id. Application rows are delivered
# to the web socket that asked, and do not use a chat id.
SLOT_SPECS: Final[Mapping[str, SlotSpec]] = MappingProxyType(
    {
        "application.entry": _slot("single", SlotScope.APPLICATION),
        "application.sidebar.footer": _slot("single", SlotScope.APPLICATION),
        "conversation.input.above": _slot("list"),
        "conversation.input.below": _slot("single"),
        "conversation.panel.right": _slot("list"),
        "application.menu": _slot("list"),
        "application.menu.main": _slot("single"),
        "task.function": _slot("list"),
        "task.function.main": _slot("single"),
        "application.settings": _slot("list"),
        "conversation.session.content.after": _slot("list"),
        "conversation.turn.footer.actions": _slot("list"),
        "conversation.header.title.after": _slot("list"),
        "conversation.header.actions": _slot("list"),
        "message.assistant.meta": _slot("list"),
    }
)

# Capacity of the conversation slots. Application slots live only in SLOT_SPECS.
SESSION_SLOTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        slot_id: spec.capacity
        for slot_id, spec in SLOT_SPECS.items()
        if spec.scope is SlotScope.SESSION
    }
)


def slot_spec(slot_id: str) -> SlotSpec | None:
    """Return the allowlisted definition, or None when the slot is unknown."""

    return SLOT_SPECS.get(slot_id)

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
CODE_CONNECTION_REQUIRED: Final[str] = "CONNECTION_REQUIRED"
CODE_CHANNEL_UNSUPPORTED: Final[str] = "CHANNEL_UNSUPPORTED"
