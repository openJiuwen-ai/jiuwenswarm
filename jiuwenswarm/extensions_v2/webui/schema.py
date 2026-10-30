# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""渲染请求校验：槽位、组件、props、规模上限（纯函数，无 IO）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_CONNECTION_REQUIRED,
    CODE_SCHEMA_ERROR,
    COMPONENT_PROPS,
    CONTAINER_COMPONENTS,
    INTERACTIVE_COMPONENTS,
    MAX_ACTION_ARGS_BYTES,
    MAX_DEPTH,
    MAX_FRAME_BYTES,
    MAX_LIST_ITEMS,
    MAX_NODES,
    MAX_PROP_DEPTH,
    MAX_TEXT_BYTES,
    STYLE_TOKENS,
    SlotScope,
    slot_spec,
)
from jiuwenswarm.extensions_v2.webui.component_rules import COMPONENT_RULES, check_value, validate_props
from jiuwenswarm.extensions_v2.webui.models import (
    RenderFrame,
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
    ValidationReport,
)


def _fail(message: str, code: str = CODE_SCHEMA_ERROR) -> ValidationReport:
    return ValidationReport(ok=False, code=code, message=message)


_OK = ValidationReport(ok=True)


def validate_slot(slot_id: str) -> ValidationReport:
    """槽位必须在白名单内。范围由白名单决定，不在这里写死名字。"""

    if slot_spec(slot_id) is None:
        return _fail(f"slot not allowed: {slot_id}")
    return _OK


def apply_slot_scope(
    target: RenderTarget,
    trusted_connection_id: str = "",
) -> tuple[RenderTarget, ValidationReport]:
    """Normalize scope from the slot table and attach the trusted connection.

    A caller-supplied scope must match the table. An empty scope means "use the
    table". Application targets drop any chat id. Their connection id comes from
    the gateway, not from the browser payload.
    """

    spec = slot_spec(target.slot_id)
    if spec is None:
        return target, _fail(f"slot not allowed: {target.slot_id}")
    declared = target.scope.strip()
    if declared and declared != spec.scope.value:
        return target, _fail("target.scope does not match the slot")
    if spec.scope is SlotScope.SESSION:
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
    """会话槽位必须有 session_id；应用槽位必须有连接号，且不能带聊天号。"""

    if not isinstance(target, RenderTarget):
        return _fail("target must be a RenderTarget")
    for name in ("session_id", "slot_id", "package_id", "instance_key"):
        value = getattr(target, name)
        if not isinstance(value, str) or "|" in value or value != value.strip():
            return _fail(f"invalid target.{name}")
        try:
            value.encode("utf-8")
        except UnicodeError:
            return _fail(f"invalid target.{name} Unicode")
    if not target.session_id:
        return _fail("target.session_id is required")
    report = validate_slot(target.slot_id)
    if not report.ok:
        return report
    spec = slot_spec(target.slot_id)
    if spec is not None and spec.scope is SlotScope.APPLICATION:
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
    """校验一次渲染请求：目标、模式与渲染树。"""

    report = validate_target(request.target)
    if not report.ok:
        return report
    if request.mode not in (RenderMode.CLEAR, RenderMode.REPLACE) or not isinstance(request.mode, RenderMode):
        return _fail("invalid render mode")
    if request.mode is RenderMode.CLEAR:
        return _OK
    if request.root is None:
        return _fail("render tree is required for replace mode")
    return validate_root(request.root)


def validate_root(root: RenderNode) -> ValidationReport:
    """深度、节点数、组件、props、动作与字节量校验。"""

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
    """整帧字节量必须落在 256 KiB 预算内（超限拒绝而非截断）。"""

    try:
        size = len(json.dumps(frame.to_payload(), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return _fail("frame must contain JSON data")
    if size > MAX_FRAME_BYTES:
        return _fail(f"frame size {size} exceeds {MAX_FRAME_BYTES}")
    return _OK


class _Counter:
    """节点累计计数。"""

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
    old_props = COMPONENT_PROPS.get(node.component)
    legacy = node.events is None and (
        node.action is not None
        or (component_rule is not None and old_props is not None
            and bool(set(node.props) - component_rule["props"].keys()) and set(node.props) <= old_props)
        or (node.component == "Tabs" and bool(node.children))
    )
    # Select the prop whitelist for action-based or event-based declarations.
    allowed_props = old_props if legacy or component_rule is None else frozenset(component_rule["props"])
    if allowed_props is None:
        return _fail(f"component not allowed: {node.component}")

    unknown = set(node.props) - allowed_props
    if unknown:
        return _fail(f"props not allowed for {node.component}: {list(unknown)}")

    if component_rule is not None and not legacy:
        error = validate_props(node.component, node.props)
        report = _fail(error) if error else _OK
    else:
        report = _validate_props(node, allowed_props)
        if report.ok and component_rule is not None and node.action is not None:
            for name, value in node.props.items():
                prop_rule = component_rule["props"].get(name)
                if prop_rule is not None and not check_value(prop_rule, value):
                    return _fail(f"invalid {node.component}.{name}")
    if not report.ok:
        return report

    if node.action is not None:
        if node.events is not None:
            return _fail("node cannot declare both action and events")
        report = _validate_action(node)
        if not report.ok:
            return report

    report = _validate_events(node)
    if not report.ok:
        return report

    if node.component in {"Panel", "Iframe"} and depth > 1:
        return _fail(f"{node.component} is only allowed at the tree root")
    if node.component == "Panel" and not node.children:
        return _fail("Panel requires body children")
    if node.component == "Tabs" and not legacy:
        for item in node.props["items"]:
            for child in item.get("children", ()):
                report = _validate_node(child, depth=depth + 1, counter=counter)
                if not report.ok:
                    return report

    if node.children:
        allows_children = (component_rule.get("children", False) if component_rule and not legacy
                           else node.component in CONTAINER_COMPONENTS)
        if not allows_children:
            return _fail(f"component {node.component} does not accept children")
        for child in node.children:
            if (component_rule and not legacy and component_rule.get("childComponents")
                    and (not isinstance(child, RenderNode) or child.component not in component_rule["childComponents"])):
                return _fail(f"invalid child component for {node.component}")
            report = _validate_node(child, depth=depth + 1, counter=counter)
            if not report.ok:
                return report
    return _OK


def _validate_props(node: RenderNode, allowed_props: frozenset[str]) -> ValidationReport:
    for name, value in node.props.items():
        if name in STYLE_TOKENS:
            if not isinstance(value, str) or value not in STYLE_TOKENS[name]:
                return _fail(f"invalid {name} token: {value!r}")
            continue
        report = _validate_value(name, value)
        if not report.ok:
            return report
    return _OK


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
        return _OK if abs(value) <= 1.7976931348623157e308 else _fail(f"prop {name} must be finite")
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


def _validate_action(node: RenderNode) -> ValidationReport:
    if node.component not in INTERACTIVE_COMPONENTS:
        return _fail(f"component {node.component} does not accept an action")
    action = node.action
    if not isinstance(action, Mapping) or set(action) - {"id", "args"}:
        return _fail("action must declare id and optional args")
    action_id = action.get("id")
    if not isinstance(action_id, str) or not action_id.strip():
        return _fail("action.id is required")
    if len(action_id.encode("utf-8")) > 200:
        return _fail("action.id too long")
    args = action.get("args")
    if args is not None:
        if not isinstance(args, Mapping):
            return _fail("action.args must be an object")
        report = _validate_value("action.args", args)
        if not report.ok:
            return report
        size = len(json.dumps(dict(args), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size > MAX_ACTION_ARGS_BYTES:
            return _fail(f"action.args exceeds {MAX_ACTION_ARGS_BYTES} bytes")
    return _OK


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

