# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validate render slots, components, properties and budgets without I/O."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_CONNECTION_REQUIRED,
    CODE_SCHEMA_ERROR,
    MAX_ACTION_ARGS_BYTES,
    MAX_DEPTH,
    MAX_FRAME_BYTES,
    MAX_LIST_ITEMS,
    MAX_NODES,
    MAX_PROP_DEPTH,
    MAX_TEXT_BYTES,
    SlotScope,
    slot_spec,
)
from jiuwenswarm.extensions_v2.webui.component_rules import COMPONENT_RULES, finite_number, validate_props
from jiuwenswarm.extensions_v2.webui.models import (
    RenderFrame,
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
    ValidationReport,
)

_SLOT_ROOT_COMPONENTS: Mapping[str, tuple[str, ...]] = {
    "conversation.panel.right": ("Panel", "Iframe"),
    "application.menu.main": ("Iframe",),
    "task.function.main": ("Iframe",),
}
_ROOT_ONLY_COMPONENTS: frozenset[str] = frozenset({"Panel", "Iframe"})


def _fail(message: str, code: str = CODE_SCHEMA_ERROR) -> ValidationReport:
    return ValidationReport(ok=False, code=code, message=message)


_OK = ValidationReport(ok=True)


def validate_slot(slot_id: str) -> ValidationReport:
    """Require a registered slot; the catalog determines its scope."""

    if slot_spec(slot_id) is None:
        return _fail(f"slot not allowed: {slot_id}")
    return _OK


def apply_slot_scope(
    target: RenderTarget,
    trusted_connection_id: str = "",
) -> tuple[RenderTarget, ValidationReport]:
    """Normalize scope from the slot table and attach the trusted connection.

    A caller-supplied scope must be allowed by the table. An empty scope means "use the
    table". Application targets drop any chat id. Their connection id comes from
    the gateway, not from the browser payload.
    """

    spec = slot_spec(target.slot_id)
    if spec is None:
        return target, _fail(f"slot not allowed: {target.slot_id}")
    declared = target.scope.strip()
    if declared and not spec.accepts_scope(declared):
        return target, _fail("target.scope does not match the slot")
    if target.resolved_scope == SlotScope.SESSION.value:
        normalized = replace(target, scope=SlotScope.SESSION.value, connection_id="")
        return normalized, validate_target(normalized)

    trusted = trusted_connection_id.strip()
    supplied = target.connection_id.strip()
    if trusted and supplied and trusted != supplied:
        return target, _fail("target.connection_id does not match the caller")
    normalized = replace(
        target,
        scope=SlotScope.APPLICATION.value,
        session_id="",
        connection_id=trusted or supplied,
    )
    return normalized, validate_target(normalized)


def validate_target(target: RenderTarget) -> ValidationReport:
    """Require a session for session targets or a connection without a chat for application targets."""

    if not isinstance(target, RenderTarget):
        return _fail("target must be a RenderTarget")
    for name in ("session_id", "slot_id", "package_id", "instance_key", "scope", "connection_id"):
        value = getattr(target, name)
        if not isinstance(value, str) or "|" in value or value != value.strip():
            return _fail(f"invalid target.{name}")
        try:
            value.encode("utf-8")
        except UnicodeError:
            return _fail(f"invalid target.{name} Unicode")
    report = validate_slot(target.slot_id)
    if not report.ok:
        return report
    spec = slot_spec(target.slot_id)
    if spec is not None and not spec.accepts_scope(target.resolved_scope):
        return _fail("target.scope does not match the slot")
    if target.resolved_scope == SlotScope.APPLICATION.value:
        if target.session_id.strip():
            return _fail("application slot must not carry session_id")
        if not target.connection_id.strip():
            return _fail("connection_id is required", CODE_CONNECTION_REQUIRED)
    elif not target.session_id.strip():
        return _fail("target.session_id is required")
    for field_name, value in (
        ("package_id", target.package_id),
        ("instance_key", target.instance_key),
        ("connection_id", target.connection_id),
    ):
        if len(value.encode("utf-8")) > 200:
            return _fail(f"target.{field_name} too long")
    return _OK


def validate_request(request: UiRenderInput) -> ValidationReport:
    """Validate a render request target, mode and tree."""

    report = validate_target(request.target)
    if not report.ok:
        return report
    if request.mode not in (RenderMode.CLEAR, RenderMode.REPLACE) or not isinstance(request.mode, RenderMode):
        return _fail("invalid render mode")
    if request.mode is RenderMode.CLEAR:
        if request.visible_when is not None:
            return _fail("clear mode must not include visible_when")
        return _OK
    binding = request.visible_when
    if binding is not None:
        if (not isinstance(binding, Mapping) or set(binding) != {"key", "default"}
                or not isinstance(binding["key"], str) or not binding["key"].strip()
                or type(binding["default"]) is not bool or not request.target.package_id):
            return _fail("invalid visible_when preference binding")
        try:
            if len(binding["key"].encode("utf-8")) > 128:
                return _fail("visible_when key exceeds 128 UTF-8 bytes")
        except UnicodeError:
            return _fail("invalid visible_when key Unicode")
    if request.root is None:
        return _fail("render tree is required for replace mode")
    report = validate_root(request.root)
    if not report.ok:
        return report
    slot_roots = _SLOT_ROOT_COMPONENTS.get(request.target.slot_id, ())
    if request.target.slot_id == "conversation.panel.right":
        if request.root.component not in slot_roots:
            return _fail(f"right panel requires one of: {', '.join(slot_roots)}")
    elif request.root.component in _ROOT_ONLY_COMPONENTS and request.root.component not in slot_roots:
        return _fail(f"{request.root.component} is only allowed in slots that accept it as the render root")
    return _OK


def validate_root(root: RenderNode) -> ValidationReport:
    """Validate tree depth, node count, components, properties, actions and text budgets."""

    counter = _Counter()
    try:
        report = _validate_node(root, depth=1, counter=counter)
    except (ValueError, TypeError, RecursionError):
        return _fail("render tree contains invalid data")
    if not report.ok:
        return report
    if counter.nodes > MAX_NODES:
        return _fail(f"node count exceeds {MAX_NODES}")
    return _OK


def frame_payload_bytes(frame: RenderFrame) -> ValidationReport:
    """Reject frames exceeding the 256 KiB budget instead of truncating them."""

    try:
        size = len(json.dumps(frame.to_payload(), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return _fail("frame must contain JSON data")
    if size > MAX_FRAME_BYTES:
        return _fail(f"frame size {size} exceeds {MAX_FRAME_BYTES}")
    return _OK


class _Counter:
    """Accumulate the render node count."""

    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0


def _validate_node(node: RenderNode, *, depth: int, counter: _Counter) -> ValidationReport:
    if not isinstance(node, RenderNode) or not isinstance(node.component, str) or not isinstance(node.props, Mapping):
        return _fail("invalid render node")
    if not isinstance(node.children, (list, tuple)):
        return _fail("children must be an array")
    if depth > MAX_DEPTH:
        return _fail(f"tree depth exceeds {MAX_DEPTH}")
    counter.nodes += 1
    if counter.nodes > MAX_NODES:
        return _fail(f"node count exceeds {MAX_NODES}")

    component_rule = COMPONENT_RULES.get(node.component)
    if component_rule is None:
        return _fail(f"component not allowed: {node.component}")

    allowed_props = frozenset(component_rule["props"])
    unknown = set(node.props) - allowed_props
    if unknown:
        return _fail(f"props not allowed for {node.component}: {list(unknown)}")

    error = validate_props(node.component, node.props)
    if error:
        return _fail(error)

    report = _validate_events(node)
    if not report.ok:
        return report

    if node.component in {"Panel", "Iframe"} and depth > 1:
        return _fail(f"{node.component} is only allowed at the tree root")
    if node.component == "Panel" and not node.children:
        return _fail("Panel requires body children")
    if node.component == "Dropdown":
        if (bool(node.props.get("icon")) == bool(node.children)
                or (not node.props.get("icon") and len(node.children) != 1)):
            return _fail("Dropdown requires exactly one trigger source: icon or non-empty children")
        if any(not _display_only(child) for child in node.children):
            return _fail("Dropdown trigger must contain only display components")
    if node.component == "Divider" and node.children:
        if "children" in node.props:
            return _fail("Divider title sources are mutually exclusive")
        if node.props.get("orientation") == "vertical":
            return _fail("vertical Divider cannot have a title")
    if node.component == "Tabs":
        for item in node.props["items"]:
            for child in item.get("children", ()):
                report = _validate_node(child, depth=depth + 1, counter=counter)
                if not report.ok:
                    return report

    if node.children:
        if not component_rule.get("children", False):
            return _fail(f"component {node.component} does not accept children")
        for child in node.children:
            if (component_rule.get("childComponents")
                    and (not isinstance(child, RenderNode) or child.component not in component_rule["childComponents"])):
                return _fail(f"invalid child component for {node.component}")
            report = _validate_node(child, depth=depth + 1, counter=counter)
            if not report.ok:
                return report
    return _OK


def _display_only(node: RenderNode) -> bool:
    return (isinstance(node, RenderNode) and node.component in {"Box", "Text", "Avatar", "Image", "Tag"}
            and not node.events and (node.component != "Image" or node.props.get("preview") is False)
            and all(_display_only(child) for child in node.children))


def _validate_value(name: str, value: Any, *, depth: int = 0) -> ValidationReport:
    if depth > MAX_PROP_DEPTH:
        return _fail(f"prop {name} exceeds depth {MAX_PROP_DEPTH}")
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            return _fail(f"prop {name} has invalid Unicode")
        if size > MAX_TEXT_BYTES:
            return _fail(f"prop {name} exceeds {MAX_TEXT_BYTES} bytes")
        return _OK
    if value is None or isinstance(value, bool):
        return _OK
    if type(value) in (int, float):
        return _OK if finite_number(value) else _fail(f"prop {name} must be finite")
    if isinstance(value, Mapping):
        if len(value) > MAX_LIST_ITEMS:
            return _fail(f"prop {name} has too many fields")
        for key, entry in value.items():
            if not isinstance(key, str):
                return _fail(f"prop {name} object keys must be strings")
            report = _validate_value(name, key, depth=depth + 1)
            if not report.ok:
                return report
            report = _validate_value(name, entry, depth=depth + 1)
            if not report.ok:
                return report
        return _OK
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > MAX_LIST_ITEMS:
            return _fail(f"prop {name} exceeds {MAX_LIST_ITEMS} items")
        for item in value:
            report = _validate_value(name, item, depth=depth + 1)
            if not report.ok:
                return report
        return _OK
    return _fail(f"prop {name} type not supported: {type(value).__name__}")


def _validate_events(node: RenderNode) -> ValidationReport:
    if node.events is None:
        return _OK
    if not isinstance(node.events, Mapping) or not node.events:
        return _fail("events must be a non-empty object")
    allowed = COMPONENT_RULES.get(node.component, {}).get("events", {})
    for event_name, declaration in node.events.items():
        if event_name not in allowed:
            return _fail(f"event not allowed for {node.component}: {event_name}")
        if not isinstance(declaration, Mapping) or set(declaration) - {"id", "args"}:
            return _fail("event must declare id and optional args")
        action_id = declaration.get("id")
        if not isinstance(action_id, str) or not action_id.strip() or len(action_id.encode("utf-8")) > 200:
            return _fail("event id is required and limited to 200 bytes")
        args = declaration.get("args", {})
        # A missing args field means {}; explicit null is not an object.
        if not isinstance(args, Mapping):
            return _fail("event args must be an object")
        report = _validate_value("event.args", args)
        if not report.ok:
            return report
        if len(json.dumps(dict(args), ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_ACTION_ARGS_BYTES:
            return _fail("event args too large")
    return _OK

