# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registry of file-oriented tools: argument name and read/write action."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

FileAction = Literal["read", "write", "exec"]


@dataclass(frozen=True)
class FileToolSpec:
    tool_name: str
    arg_name: str
    action: FileAction


_FILE_TOOL_REGISTRY: dict[str, list[FileToolSpec]] = {}


def register_file_tool(spec: FileToolSpec) -> None:
    """Register or extend specs for a tool (last registration wins for same arg)."""
    lst = _FILE_TOOL_REGISTRY.setdefault(spec.tool_name, [])
    # Replace same arg_name
    filtered = [s for s in lst if s.arg_name != spec.arg_name]
    filtered.append(spec)
    _FILE_TOOL_REGISTRY[spec.tool_name] = filtered


def lookup_file_tool_specs(tool_name: str) -> list[FileToolSpec] | None:
    """Return registered specs, or None if none."""
    specs = _FILE_TOOL_REGISTRY.get(tool_name)
    return list(specs) if specs else None


def _bootstrap_builtin_specs() -> None:
    if _FILE_TOOL_REGISTRY:
        return
    builtins: list[FileToolSpec] = [
        FileToolSpec("read_file", "file_path", "read"),
        FileToolSpec("read_file", "path", "read"),
        FileToolSpec("write_file", "file_path", "write"),
        FileToolSpec("write_file", "path", "write"),
        FileToolSpec("edit_file", "file_path", "write"),
        FileToolSpec("read_text_file", "file_path", "read"),
        FileToolSpec("write_text_file", "file_path", "write"),
        FileToolSpec("write", "file_path", "write"),
        FileToolSpec("read", "file_path", "read"),
        FileToolSpec("Write", "file_path", "write"),
        FileToolSpec("Write", "path", "write"),
        FileToolSpec("Write", "target_file", "write"),
        FileToolSpec("Read", "file_path", "read"),
        FileToolSpec("Read", "path", "read"),
        FileToolSpec("Edit", "file_path", "write"),
        FileToolSpec("Edit", "path", "write"),
        FileToolSpec("Edit", "target_file", "write"),
        FileToolSpec("search_replace", "file_path", "write"),
        FileToolSpec("search_replace", "path", "write"),
        FileToolSpec("search_replace", "target_file", "write"),
        FileToolSpec("grep", "path", "read"),
        FileToolSpec("grep", "file_path", "read"),
        FileToolSpec("grep_search", "path", "read"),
        FileToolSpec("glob_file_search", "glob_pattern", "read"),
        FileToolSpec("glob", "pattern", "read"),
        FileToolSpec("list_dir", "path", "read"),
        FileToolSpec("list_files", "path", "read"),
    ]
    for s in builtins:
        register_file_tool(s)


_bootstrap_builtin_specs()
