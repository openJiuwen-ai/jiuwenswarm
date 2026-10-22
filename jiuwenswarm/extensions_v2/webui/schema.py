# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""渲染请求校验：槽位、组件、props、规模上限（纯函数，无 IO）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jiuwenswarm.extensions_v2.webui.constants import (
    CODE_SCHEMA_ERROR,
    COMPONENT_PROPS,
    CONTAINER_COMPONENTS,
    INTERACTIVE_COMPONENTS,
    MAX_ACTION_ARGS_BYTES,
    MAX_DEPTH,
    MAX_FRAME_BYTES,
    MAX_LIST_ITEMS,
    MAX_NODES,
    MAX_TEXT_BYTES,
    SESSION_SLOTS,
    STYLE_TOKENS,
)
from jiuwenswarm.extensions_v2.webui.models import (
    RenderFrame,
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
    ValidationReport,
)

_SCALARS = (str, int, float, bool)


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

    if not target.session_id.strip():
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
    if request.mode is RenderMode.CLEAR:
        return _OK
    if request.root is None:
        return _fail("render tree is required for replace mode")
    return validate_root(request.root)


def validate_root(root: RenderNode) -> ValidationReport:
    """深度、节点数、组件、props、动作与字节量校验。"""

    counter = _Counter()
    report = _validate_node(root, depth=1, counter=counter)
    if not report.ok:
        return report
    if counter.nodes > MAX_NODES:
        return _fail(f"node count exceeds {MAX_NODES}")
    return _OK


def frame_payload_bytes(frame: RenderFrame) -> ValidationReport:
    """整帧字节量必须落在 256 KiB 预算内（超限拒绝而非截断）。"""

    size = len(json.dumps(frame.to_payload(), ensure_ascii=False).encode("utf-8"))
    if size > MAX_FRAME_BYTES:
        return _fail(f"frame size {size} exceeds {MAX_FRAME_BYTES}")
    return _OK


class _Counter:
    """节点累计计数。"""

    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0


def _validate_node(node: RenderNode, *, depth: int, counter: _Counter) -> ValidationReport:
    if depth > MAX_DEPTH:
        return _fail(f"tree depth exceeds {MAX_DEPTH}")
    counter.nodes += 1
    if counter.nodes > MAX_NODES:
        return _fail(f"node count exceeds {MAX_NODES}")

    allowed_props = COMPONENT_PROPS.get(node.component)
    if allowed_props is None:
        return _fail(f"component not allowed: {node.component}")

    unknown = set(node.props) - allowed_props
    if unknown:
        return _fail(f"props not allowed for {node.component}: {sorted(unknown)}")

    report = _validate_props(node, allowed_props)
    if not report.ok:
        return report

    if node.action is not None:
        report = _validate_action(node)
        if not report.ok:
            return report

    if node.children:
        if node.component not in CONTAINER_COMPONENTS:
            return _fail(f"component {node.component} does not accept children")
        for child in node.children:
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


def _validate_value(name: str, value: Any) -> ValidationReport:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            return _fail(f"prop {name} exceeds {MAX_TEXT_BYTES} bytes")
        return _OK
    if value is None or isinstance(value, (bool, int, float)):
        return _OK
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > MAX_LIST_ITEMS:
            return _fail(f"prop {name} exceeds {MAX_LIST_ITEMS} items")
        for item in value:
            if isinstance(item, Mapping):
                for key, entry in item.items():
                    if not isinstance(key, str) or not _is_scalar(entry):
                        return _fail(f"prop {name} item field not supported: {key!r}")
                continue
            if not _is_scalar(item):
                return _fail(f"prop {name} item not supported: {item!r}")
        return _OK
    return _fail(f"prop {name} type not supported: {type(value).__name__}")


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, _SCALARS)


def _validate_action(node: RenderNode) -> ValidationReport:
    if node.component not in INTERACTIVE_COMPONENTS:
        return _fail(f"component {node.component} does not accept an action")
    action = node.action or {}
    action_id = action.get("id")
    if not isinstance(action_id, str) or not action_id.strip():
        return _fail("action.id is required")
    if len(action_id.encode("utf-8")) > 200:
        return _fail("action.id too long")
    args = action.get("args")
    if args is not None:
        if not isinstance(args, Mapping):
            return _fail("action.args must be an object")
        for key, value in args.items():
            if not isinstance(key, str) or not (_is_scalar(value) or isinstance(value, Sequence)):
                return _fail(f"action.args field not supported: {key!r}")
        size = len(json.dumps(dict(args), ensure_ascii=False).encode("utf-8"))
        if size > MAX_ACTION_ARGS_BYTES:
            return _fail(f"action.args exceeds {MAX_ACTION_ARGS_BYTES} bytes")
    return _OK

