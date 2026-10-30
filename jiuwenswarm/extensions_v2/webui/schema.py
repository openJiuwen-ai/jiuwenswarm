# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""渲染请求校验：槽位、组件、props、规模上限（纯函数，无 IO）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_SCHEMA_ERROR,
    MAX_ACTION_ARGS_BYTES,
    MAX_DEPTH,
    MAX_FRAME_BYTES,
    MAX_LIST_ITEMS,
    MAX_NODES,
    MAX_PROP_DEPTH,
    MAX_TEXT_BYTES,
    SESSION_SLOTS,
)
from jiuwenswarm.extensions_v2.webui.component_rules import COMPONENT_RULES, validate_props
from jiuwenswarm.extensions_v2.webui.models import (
    RenderFrame,
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
    ValidationReport,
)


def _fail(message: str) -> ValidationReport:
    return ValidationReport(ok=False, code=CODE_SCHEMA_ERROR, message=message)


_OK = ValidationReport(ok=True)


def validate_slot(slot_id: str) -> ValidationReport:
    """槽位必须在会话语境白名单内。"""

    if slot_id not in SESSION_SLOTS:
        return _fail(f"slot not allowed: {slot_id}")
    return _OK


def validate_target(target: RenderTarget) -> ValidationReport:
    """目标必须携带 session_id 与合法槽位。"""

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
    for field_name, value in (("package_id", target.package_id), ("instance_key", target.instance_key)):
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

