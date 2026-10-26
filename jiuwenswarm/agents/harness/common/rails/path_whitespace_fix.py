# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Repair file-tool paths that differ from a real path only by whitespace."""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

# Built-in read-side tools that work on the local filesystem. Write/edit tools
# are left out on purpose: a missing path there may be a file to create.
# ACP's read_text_file is left out too: it is served by the client (fs/read_text_file
# JSON-RPC), so the path lives on the client's filesystem and os.path.exists on the
# host cannot tell whether it is missing.
WHITESPACE_FIX_TOOL_NAMES = frozenset({"read_file", "list_files", "glob", "grep"})

_SINGLE_PATH_KEYS = ("file_path", "path")
_MULTI_PATH_KEYS = ("file_paths",)


def _squash(name: str) -> str:
    """Drop every whitespace character (``str.split`` covers full-width spaces)."""
    return "".join(name.split())


def _is_unc_path(path: str) -> bool:
    return path.startswith(("\\\\", "//"))


def resolve_whitespace_variant(path: str, base_dir: str | None = None) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    expanded = os.path.expanduser(path.strip())
    if _is_unc_path(expanded):
        return None
    if not os.path.isabs(expanded):
        if not base_dir:
            return None
        expanded = os.path.join(base_dir, expanded)
    if os.path.exists(expanded):
        return None

    parts = pathlib.PurePath(expanded).parts
    if len(parts) < 2 or not os.path.isdir(parts[0]):
        return None

    current = parts[0]
    changed = False
    for part in parts[1:]:
        candidate = os.path.join(current, part)
        if os.path.exists(candidate):
            current = candidate
            continue
        key = _squash(part)
        if not key:
            return None
        try:
            names = os.listdir(current)
        except OSError:
            return None
        matches = [name for name in names if _squash(name) == key]
        if len(matches) != 1:
            return None
        current = os.path.join(current, matches[0])
        changed = True
    return current if changed else None


def fix_whitespace_path_arguments(
    arguments: Any,
    base_dir: str | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]]] | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(arguments, dict):
        return None

    new_args = dict(arguments)
    fixes: list[tuple[str, str]] = []

    for key in _SINGLE_PATH_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        corrected = resolve_whitespace_variant(value, base_dir)
        if corrected:
            new_args[key] = corrected
            fixes.append((value, corrected))

    for key in _MULTI_PATH_KEYS:
        values = arguments.get(key)
        if not isinstance(values, list):
            continue
        new_values = list(values)
        for index, value in enumerate(values):
            if not isinstance(value, str):
                continue
            corrected = resolve_whitespace_variant(value, base_dir)
            if corrected:
                new_values[index] = corrected
                fixes.append((value, corrected))
        new_args[key] = new_values

    if not fixes:
        return None
    return new_args, fixes


def build_whitespace_fix_notice(fixes: list[tuple[str, str]], language: str = "cn") -> str:
    lines = []
    for requested, corrected in fixes:
        if language == "en":
            lines.append(
                f"[Path corrected] '{requested}' does not exist. Used the only entry that differs "
                f"by whitespace alone: '{corrected}'. The result below is for the corrected path; "
                "keep using it and do not retry the original path."
            )
        else:
            lines.append(
                f"[路径已自动纠正] 请求的路径不存在：'{requested}'。已改用同目录下仅空白字符不同的唯一匹配："
                f"'{corrected}'。下面是纠正后路径的结果，后续请直接使用该路径，不要重试原路径。"
            )
    return "\n".join(lines)


__all__ = [
    "WHITESPACE_FIX_TOOL_NAMES",
    "build_whitespace_fix_notice",
    "fix_whitespace_path_arguments",
    "resolve_whitespace_variant",
]
