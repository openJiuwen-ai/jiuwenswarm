# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Format A2UI responses into plain text for fallback paths."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from a2ui.schema.constants import A2UI_OPEN_TAG

from jiuwenswarm.server.runtime.a2ui.parser import strip_tagged_a2ui_blocks
from jiuwenswarm.server.runtime.a2ui.types import A2UIResponsePart, A2UIValidationResult


def _literal_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in (
        "literalString",
        "literalNumber",
        "literalBoolean",
        "valueString",
        "valueNumber",
        "valueBoolean",
    ):
        if key in value:
            return str(value[key])
    return None


def _extract_component_text(component_node: dict[str, Any]) -> list[str]:
    component = component_node.get("component")
    if not isinstance(component, dict):
        return []

    snippets: list[str] = []
    for component_name, props in component.items():
        if not isinstance(props, dict):
            continue
        if component_name == "Text":
            text = _literal_value(props.get("text"))
            if text:
                snippets.append(text)
        elif component_name in {"TextField", "CheckBox", "Slider"}:
            label = _literal_value(props.get("label"))
            if label:
                snippets.append(label)
        elif component_name == "MultipleChoice":
            for option in props.get("options") or []:
                if isinstance(option, dict):
                    label = _literal_value(option.get("label"))
                    if label:
                        snippets.append(label)
        elif component_name == "Button":
            action = props.get("action")
            if isinstance(action, dict) and action.get("name"):
                snippets.append(f"Action: {action['name']}")
    return snippets


def _summarize_messages(messages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for message in messages:
        if "beginRendering" in message:
            surface_id = message["beginRendering"].get("surfaceId", "default")
            lines.append(f"A2UI surface: {surface_id}")
        elif "surfaceUpdate" in message:
            components = message["surfaceUpdate"].get("components") or []
            for component in components:
                if isinstance(component, dict):
                    lines.extend(_extract_component_text(component))
        elif "dataModelUpdate" in message:
            for entry in message["dataModelUpdate"].get("contents") or []:
                if not isinstance(entry, dict):
                    continue
                value = _literal_value(entry)
                if value:
                    lines.append(f"{entry.get('key')}: {value}")
        elif "deleteSurface" in message:
            surface_id = message["deleteSurface"].get("surfaceId", "default")
            lines.append(f"A2UI surface deleted: {surface_id}")
    return lines


def format_for_text_channel(
    content: str,
    *,
    parse_response: Callable[[str], list[A2UIResponsePart]],
    validate_response: Callable[[str], A2UIValidationResult],
) -> str:
    if A2UI_OPEN_TAG in (content or ""):
        validation = validate_response(content)
        if not validation.valid:
            return strip_tagged_a2ui_blocks(content)
    try:
        parts = parse_response(content)
    except Exception:
        return ""
    lines: list[str] = []
    for part in parts:
        if part.kind == "text" and part.text.strip():
            lines.append(part.text.strip())
            continue
        if part.kind != "a2ui":
            continue
        summary = _summarize_messages(part.messages or [])
        if summary:
            lines.extend(summary)
    return "\n".join(line for line in lines if line).strip()


__all__ = ["format_for_text_channel"]
