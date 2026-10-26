# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Protect user-visible SkillTurbo markdown from glued/unclosed fences.

LLM JSON often arrives as ```json ... ```. Consecutive chat.delta pieces are
concatenated without a separator. A closing fence plus the next opening fence
becomes one line of six backticks (``````json), which CommonMark does not treat
as a closer. The rest of the execution log is then swallowed into one <pre>.
"""

from __future__ import annotations

import re

# Whole first line must be a single language tag (no spaces), so English progress
# text like "Processing results..." is not treated as ``` + json continuation.
_FENCE_INFO_CONTINUATION = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*\s*$")


def _looks_like_fence_line(line: str) -> bool:
    stripped = line.lstrip(" ")
    return stripped.startswith("```") or stripped.startswith("~~~")


def _fence_info(line: str) -> str | None:
    """Return the info string after a fence marker, or None if not a fence line."""
    stripped = line.lstrip(" ")
    if stripped.startswith("```"):
        i = 0
        while i < len(stripped) and stripped[i] == "`":
            i += 1
        return stripped[i:]
    if stripped.startswith("~~~"):
        i = 0
        while i < len(stripped) and stripped[i] == "~":
            i += 1
        return stripped[i:]
    return None


def _is_fence_info_continuation(last_line: str, incoming: str) -> bool:
    """True when last_line is a marker-only fence and incoming is a language tag.

    Streaming often splits ```json into ``` then json. A newline between those
    tokens would break the opening fence.
    """
    info = _fence_info(last_line)
    if info is None or info.strip():
        return False
    first_line = incoming.split("\n", 1)[0]
    return bool(_FENCE_INFO_CONTINUATION.match(first_line))


def terminate_dangling_markdown_fence(content: str) -> str:
    """Append a newline when content ends on a completed fence line.

    Marker-only fences (``` with no info yet) are left unchanged so a following
    ``json`` token can still complete the opening fence.
    """
    if not content or content.endswith("\n"):
        return content
    last_line = content.rsplit("\n", 1)[-1]
    info = _fence_info(last_line)
    if info is None:
        return content
    if not info.strip():
        return content
    return content + "\n"


def markdown_stream_incoming(previous: str, incoming: str) -> str:
    """Prefix incoming with a newline when concatenating would glue a fence."""
    if not previous or not incoming:
        return incoming
    if previous.endswith("\n") or incoming.startswith("\n"):
        return incoming
    last_line = previous.rsplit("\n", 1)[-1]
    first_line = incoming.split("\n", 1)[0]
    if _looks_like_fence_line(first_line):
        return "\n" + incoming
    if _looks_like_fence_line(last_line) and not _is_fence_info_continuation(
        last_line, incoming
    ):
        return "\n" + incoming
    return incoming
