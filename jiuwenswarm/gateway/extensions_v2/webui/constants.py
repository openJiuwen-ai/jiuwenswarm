# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI extension constants: events, slots and size limits.

Copied from ``jiuwenswarm/extensions_v2/webui/constants.py`` with the slot table
narrowed to Gateway-owned application slots.
"""

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
FRAME_APPEARANCE: Final[str] = "webui.appearance"

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


# Gateway allowlist. Session slots stay owned by AgentServer: the Gateway only
# renders application-scoped slots it can address by web socket.
# ``application.entry`` is the full-screen host the browser already mounts;
# ``application.sidebar.footer`` is a list slot for several packages' footer
# content and does not depend on a chat session.
# ``application.background`` is the full-window layer below the workspace; it
# only accepts a ``MediaSurface`` root.
SLOT_SPECS: Final[Mapping[str, SlotSpec]] = MappingProxyType(
    {
        "application.entry": _slot("single", SlotScope.APPLICATION),
        "application.sidebar.footer": _slot("list", SlotScope.APPLICATION),
        "application.background": _slot("single", SlotScope.APPLICATION),
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
