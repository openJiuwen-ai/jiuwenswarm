# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validate upstream interactions before entering any business event handler."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.extensions_v2.webui.component_rules import finite_number, validate_event_data
from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_SCHEMA_ERROR,
    CODE_TARGET_INACTIVE,
    MAX_FRAME_BYTES,
    SLOT_READY_EVENT,
    UPSTREAM_EVENTS,
)
from jiuwenswarm.extensions_v2.webui.models import RenderMode, RenderNode, UiActionInput, UiActionOutput, _content_to_payload
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry
from jiuwenswarm.extensions_v2.webui.schema import _validate_value, validate_target

_PAYLOAD_FIELDS = frozenset({"action_id", "args", "data", "node_key", "revision"})
_LEGACY_EVENTS = {"Button": "ui.press", "TextField": "ui.input", "CheckBox": "ui.toggle",
                  "MultipleChoice": "ui.select", "Slider": "ui.select", "List": "ui.press"}


def validate_upstream(request: UiActionInput, registry: ActiveRenderRegistry) -> UiActionOutput | None:
    """Return a rejection receipt, or None when the event chain may execute."""
    invalid = UiActionOutput(False, "invalid_action", CODE_SCHEMA_ERROR)
    if not isinstance(request, UiActionInput):
        return invalid
    report = validate_target(request.target)
    if (not report.ok or not isinstance(request.event_type, str) or request.event_type not in UPSTREAM_EVENTS
            or not isinstance(request.payload, Mapping)):
        return invalid
    if request.event_type == SLOT_READY_EVENT:
        return None if not request.payload else invalid
    if not request.target.package_id:
        return invalid
    instance = registry.get(request.target)
    if instance is None or instance.mode is not RenderMode.REPLACE or instance.root is None:
        return UiActionOutput(False, "inactive", CODE_TARGET_INACTIVE)
    payload = request.payload
    if set(payload) != _PAYLOAD_FIELDS:
        return invalid
    if type(payload["revision"]) is not int or payload["revision"] != instance.revision:
        return UiActionOutput(False, "stale", CODE_TARGET_INACTIVE)
    node = _find_node(instance.root, payload["node_key"])
    if node is None:
        return UiActionOutput(False, "inactive", CODE_TARGET_INACTIVE)
    if not _validate_value("payload", payload).ok:
        return invalid
    try:
        if len(json.dumps(_content_to_payload(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_FRAME_BYTES:
            return invalid
    except (TypeError, ValueError):
        return invalid
    if node.events is not None:
        declaration = node.events.get(request.event_type)
    else:
        declaration = node.action if _LEGACY_EVENTS.get(node.component) == request.event_type else None
    if not isinstance(declaration, Mapping) or type(payload["action_id"]) is not str:
        return invalid
    if payload["action_id"] != declaration.get("id") or not isinstance(payload["args"], Mapping):
        return invalid
    # Keep bool distinct from numbers and treat tuple/list as the same wire array.
    expected_args = declaration.get("args") or {}
    if not _same_json(payload["args"], expected_args):
        return invalid
    data = payload["data"]
    if not isinstance(data, Mapping):
        return invalid
    valid = (validate_event_data(node.component, request.event_type, data, node.props) if node.events is not None
             else _legacy_data_allowed(node, data))
    return None if valid else invalid


def _same_json(actual: Any, expected: Any) -> bool:
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return actual.keys() == expected.keys() and all(_same_json(actual[key], expected[key]) for key in actual)
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(_same_json(a, b) for a, b in zip(actual, expected))
    if finite_number(actual) and finite_number(expected):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _find_node(root: RenderNode, node_key: Any) -> RenderNode | None:
    """Resolve canonical browser paths, including only Tabs.items[].children[]."""
    if not isinstance(node_key, str) or len(node_key) > 200:
        return None
    parts = node_key.split(".")
    if parts[0] != "0":
        return None
    node = root
    position = 1
    while position < len(parts):
        segment = parts[position]
        if re.fullmatch(r"0|[1-9][0-9]{0,2}", segment):
            index = int(segment)
            if index >= len(node.children):
                return None
            node = node.children[index]
            position += 1
        elif (node.component == "Tabs" and segment == "items" and position + 3 < len(parts)
              and parts[position + 2] == "children"):
            item_index, child_index = parts[position + 1], parts[position + 3]
            if not all(re.fullmatch(r"0|[1-9][0-9]{0,2}", item) for item in (item_index, child_index)):
                return None
            items = node.props.get("items", ())
            if int(item_index) >= len(items):
                return None
            item = items[int(item_index)]
            children = item.get("children", ())
            if item.get("disabled") or int(child_index) >= len(children):
                return None
            node = children[int(child_index)]
            position += 4
        else:
            return None
    return node


def _legacy_data_allowed(node: RenderNode, data: Mapping[str, Any]) -> bool:
    """Validate the existing action API without translating it into component events."""
    props = node.props
    if props.get("disabled"):
        return False
    if node.component == "Button":
        return not data
    if node.component == "List":
        return set(data) == {"key"} and any(item.get("key") == data["key"] for item in props.get("items", ()))
    if not isinstance(data.get("name"), str) or data["name"] != props.get("name", ""):
        return False
    if node.component == "CheckBox":
        return set(data) == {"name", "checked"} and type(data["checked"]) is bool
    if set(data) != {"name", "value"}:
        return False
    value = data["value"]
    if node.component == "TextField":
        limit = props.get("max_length", 0)
        return isinstance(value, str) and (not limit or len(value.encode("utf-16-le")) // 2 <= limit)
    if node.component == "Slider":
        from jiuwenswarm.extensions_v2.webui.component_rules import slider_value_allowed

        return slider_value_allowed(props, value)
    if node.component == "MultipleChoice":
        values = [item.get("key") for item in props.get("options", ()) if not item.get("disabled")]
        return (isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)
                and len(set(value)) == len(value) and (props.get("multiple") or len(value) <= 1)
                and all(item in values for item in value))
    return False
