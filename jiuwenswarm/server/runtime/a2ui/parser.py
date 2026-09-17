# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A2UI response parsing helpers."""

from __future__ import annotations

import copy
import json
from typing import Any

from a2ui.parser.parser import parse_response as parse_tagged_response
from a2ui.schema.constants import A2UI_CLOSE_TAG, A2UI_OPEN_TAG

from jiuwenswarm.server.runtime.a2ui.types import A2UIResponsePart


_A2UI_MESSAGE_KEYS = frozenset(
    {"beginRendering", "surfaceUpdate", "dataModelUpdate", "deleteSurface"}
)


def is_a2ui_message(value: Any) -> bool:
    return isinstance(value, dict) and len(_A2UI_MESSAGE_KEYS.intersection(value)) == 1


def coerce_message_list(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list) and all(is_a2ui_message(item) for item in value):
        return [copy.deepcopy(item) for item in value]
    if is_a2ui_message(value):
        return [copy.deepcopy(value)]
    return None


def iter_tagged_block_bodies(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    cursor = 0
    while True:
        start = text.find(A2UI_OPEN_TAG, cursor)
        if start < 0:
            return blocks
        body_start = start + len(A2UI_OPEN_TAG)
        end = text.find(A2UI_CLOSE_TAG, body_start)
        if end < 0:
            blocks.append((len(blocks), text[body_start:]))
            return blocks
        blocks.append((len(blocks), text[body_start:end]))
        cursor = end + len(A2UI_CLOSE_TAG)


def strip_tagged_a2ui_blocks(text: str) -> str:
    output: list[str] = []
    cursor = 0
    while True:
        start = text.find(A2UI_OPEN_TAG, cursor)
        if start < 0:
            output.append(text[cursor:])
            break
        output.append(text[cursor:start])
        end = text.find(A2UI_CLOSE_TAG, start + len(A2UI_OPEN_TAG))
        if end < 0:
            break
        cursor = end + len(A2UI_CLOSE_TAG)
    return "".join(output).strip()


def parse_raw_json(text: str) -> list[dict[str, Any]] | None:
    stripped = text.strip()
    if not stripped.startswith(("[", "{")):
        return None
    try:
        return coerce_message_list(json.loads(stripped))
    except json.JSONDecodeError:
        return None


def parse_jsonl(text: str) -> list[dict[str, Any]] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not all(line.startswith("{") and line.endswith("}") for line in lines):
        return None
    messages: list[dict[str, Any]] = []
    try:
        for line in lines:
            parsed = json.loads(line)
            if not is_a2ui_message(parsed):
                return None
            messages.append(parsed)
    except json.JSONDecodeError:
        return None
    return messages


def parse_a2ui_response(content: str) -> list[A2UIResponsePart]:
    text = content or ""
    if not text.strip():
        return []

    jsonl_messages = parse_jsonl(text)
    if jsonl_messages is not None:
        return [A2UIResponsePart(kind="a2ui", messages=jsonl_messages)]

    raw_json_messages = parse_raw_json(text)
    if raw_json_messages is not None:
        return [A2UIResponsePart(kind="a2ui", messages=raw_json_messages)]

    parts: list[A2UIResponsePart] = []
    for part in parse_tagged_response(text):
        part_text = (part.text or "").strip()
        if part_text:
            parts.append(A2UIResponsePart(kind="text", text=part_text))
        messages = coerce_message_list(part.a2ui_json)
        if messages is not None:
            parts.append(A2UIResponsePart(kind="a2ui", messages=messages))
    return parts or [A2UIResponsePart(kind="text", text=text)]


def may_contain_a2ui_content(content: str) -> bool:
    text = content or ""
    return A2UI_OPEN_TAG in text or parse_jsonl(text) is not None or parse_raw_json(text) is not None


__all__ = [
    "coerce_message_list",
    "iter_tagged_block_bodies",
    "may_contain_a2ui_content",
    "parse_a2ui_response",
    "parse_jsonl",
    "parse_raw_json",
    "strip_tagged_a2ui_blocks",
]
