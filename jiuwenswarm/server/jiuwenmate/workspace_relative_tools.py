"""Tell colleague file writes to use the office directory, and accept an in-office absolute path.

Core ``write_file`` and ``edit_file`` parameter text still asks for an absolute
path. The write check rejects every absolute path before it looks at the office
directory, so the first call fails and the model retries with a relative path.
Read tools already rewrite that text. This host patch does the same for the
persistent write adapter, and turns an absolute path that resolves inside the
office directory into the relative path the check accepts.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_REPLACEMENTS = (
    (
        "要写入的文件绝对路径",
        "办公目录内的相对路径。新建文件默认写在办公目录根下，例如 test.txt；不接受绝对路径或包含 `..` 的路径段",
    ),
    (
        "Absolute path of the file to write",
        "Path relative to the office directory. New files are created at the office root by default, "
        "for example test.txt. Absolute paths and `..` segments are not accepted",
    ),
    (
        "要编辑的文件绝对路径",
        "办公目录内要修改的文件的相对路径，例如 test.txt 或 资料/周报.md；不接受绝对路径或包含 `..` 的路径段",
    ),
    (
        "The absolute path to the file to edit",
        "Office-directory-relative path of the file to edit, for example test.txt or notes/report.md. "
        "Absolute paths and `..` segments are not accepted",
    ),
)


def install_office_relative_write_tools() -> None:
    """Rewrite persistent write-tool cards once, and relativize in-office absolute paths."""
    from openjiuwen.harness.persistent_agent.workspace.file_access import (
        PersistentWorkspaceWriteRail,
        WorkspaceWriteAdapter,
    )

    if getattr(WorkspaceWriteAdapter, "_office_relative_paths", False):
        return
    original_init = WorkspaceWriteAdapter.__init__
    original_before = PersistentWorkspaceWriteRail.before_tool_call

    def __init__(self, base_tool: Any) -> None:
        original_init(self, base_tool)
        card = self._card.model_copy(deep=True)
        apply_office_relative_path_text(card)
        self._card = card

    async def before_tool_call(self, ctx: Any) -> None:
        rewrite_office_absolute_argument(self, ctx)
        await original_before(self, ctx)

    WorkspaceWriteAdapter.__init__ = __init__
    PersistentWorkspaceWriteRail.before_tool_call = before_tool_call
    WorkspaceWriteAdapter._office_relative_paths = True


def apply_office_relative_path_text(card: Any) -> None:
    """Replace absolute-path wording on one copied write or edit card."""
    description = getattr(card, "description", None)
    if isinstance(description, str):
        card.description = _replace_text(description)
    params = getattr(card, "input_params", None)
    if not isinstance(params, dict):
        return
    properties = params.get("properties")
    if not isinstance(properties, dict):
        return
    path_param = properties.get("file_path")
    if not isinstance(path_param, dict):
        return
    path_description = path_param.get("description")
    if isinstance(path_description, str):
        path_param["description"] = _replace_text(path_description)


def rewrite_office_absolute_argument(rail: Any, ctx: Any) -> None:
    """Replace one in-office absolute ``file_path`` before the write check runs."""
    inputs = getattr(ctx, "inputs", None)
    if getattr(inputs, "tool_name", None) not in _WRITE_TOOLS:
        return
    root = getattr(getattr(rail, "workspace_access", None), "_root", None)
    if root is None:
        return
    try:
        rewritten = rewrite_tool_args(inputs.tool_args, root)
    except (OSError, TypeError, ValueError):
        return
    inputs.tool_args = rewritten


def rewrite_tool_args(tool_args: Any, workspace_root: Path | str) -> Any:
    """Return tool arguments whose ``file_path`` is office-relative when it already was inside."""
    if isinstance(tool_args, str):
        parsed = json.loads(tool_args)
        if not isinstance(parsed, dict) or not _rewrite_mapping(parsed, workspace_root):
            return tool_args
        return json.dumps(parsed, ensure_ascii=False)
    if isinstance(tool_args, dict):
        _rewrite_mapping(tool_args, workspace_root)
    return tool_args


def office_relative_file_path(requested: str, workspace_root: Path | str) -> str | None:
    """Return the office-relative form of an absolute path that resolves inside the office."""
    if not isinstance(requested, str) or not requested or "\x00" in requested:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in requested):
        return None
    normalized = requested.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(requested)
    absolute = (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or normalized.startswith("/")
    )
    if not absolute:
        return None
    try:
        root = Path(workspace_root).expanduser().resolve()
        candidate = Path(requested).expanduser().resolve()
        relative = candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if not relative.parts or any(part in ("..", ".") for part in relative.parts):
        return None
    return PurePosixPath(*relative.parts).as_posix()


def _replace_text(value: str) -> str:
    for old, new in _REPLACEMENTS:
        value = value.replace(old, new)
    return value


def _rewrite_mapping(arguments: dict[str, Any], workspace_root: Path | str) -> bool:
    requested = arguments.get("file_path")
    if not isinstance(requested, str):
        return False
    relative = office_relative_file_path(requested, workspace_root)
    if relative is None or relative == requested:
        return False
    arguments["file_path"] = relative
    return True
