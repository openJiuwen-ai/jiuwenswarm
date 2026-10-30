# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI extension constants: events, slots, component whitelist, tokens and limits."""

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
    PRESS = "ui.press"
    TOGGLE = "ui.toggle"
    INPUT = "ui.input"
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
        WebUIEvent.PRESS.value,
        WebUIEvent.TOGGLE.value,
        WebUIEvent.INPUT.value,
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
        "conversation.input.above": _slot("list"),
        "conversation.input.below": _slot("single"),
        "conversation.panel.right": _slot("list"),
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

# Component whitelist: names match the frontend component switch.
CONTAINER_COMPONENTS: Final[frozenset[str]] = frozenset({"Column", "Row", "Card", "Tabs"})
INTERACTIVE_COMPONENTS: Final[frozenset[str]] = frozenset(
    {"Button", "TextField", "CheckBox", "MultipleChoice", "Slider", "List"}
)

# Allowed props per component. Unknown props reject the whole tree.
COMPONENT_PROPS: Final[Mapping[str, frozenset[str]]] = {
    "Column": frozenset({"gap", "align", "justify", "padding", "wrap", "density"}),
    "Row": frozenset({"gap", "align", "justify", "padding", "wrap", "density"}),
    "Card": frozenset({"title", "tone", "padding", "collapsible", "density"}),
    "Tabs": frozenset({"items", "selected"}),
    "Divider": frozenset({"axis", "spacing"}),
    "Text": frozenset({"value", "variant", "tone", "size", "weight", "max_lines", "monospace"}),
    "Icon": frozenset({"name", "size", "tone"}),
    "Badge": frozenset({"label", "tone", "size"}),
    "Button": frozenset({"label", "variant", "size", "disabled"}),
    "TextField": frozenset({"name", "placeholder", "value", "max_length"}),
    "CheckBox": frozenset({"name", "label", "checked", "disabled"}),
    "MultipleChoice": frozenset({"name", "options", "value", "multiple", "disabled"}),
    "Slider": frozenset({"name", "min", "max", "step", "value", "disabled"}),
    "List": frozenset({"items", "ordered", "selected"}),
    "Progress": frozenset({"value", "tone", "label"}),
}

# Style tokens: plugins pick a token, they never write colours or pixels.
STYLE_TOKENS: Final[Mapping[str, frozenset[str]]] = {
    "variant": frozenset({"default", "primary", "secondary", "danger", "ghost"}),
    "tone": frozenset({"neutral", "info", "success", "warning", "danger"}),
    "size": frozenset({"sm", "md", "lg"}),
    "align": frozenset({"left", "center", "right"}),
    "density": frozenset({"compact", "comfortable"}),
    "gap": frozenset({"sm", "md", "lg"}),
    "weight": frozenset({"regular", "medium", "bold"}),
    "justify": frozenset({"start", "center", "end", "between"}),
    "spacing": frozenset({"none", "sm", "md", "lg"}),
    "axis": frozenset({"horizontal", "vertical"}),
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
CODE_CONNECTION_REQUIRED: Final[str] = "CONNECTION_REQUIRED"
CODE_CHANNEL_UNSUPPORTED: Final[str] = "CHANNEL_UNSUPPORTED"
