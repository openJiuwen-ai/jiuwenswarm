# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only workspace browsing for an already-bound single-Agent session."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from jiuwenswarm.common.mode_matrix import deprecate_mode, is_single_agent_mode, is_team_mode
from jiuwenswarm.common.projectless_workspace import get_registered_projectless_task_root
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata


MAX_TREE_DEPTH = 8
MAX_TREE_NODES = 512
MAX_DIRECTORY_ENTRIES = 100
MAX_EXPANDED_DIRECTORIES = 100
MAX_TREE_BYTES = 96 * 1024
MAX_PATH_LENGTH = 1024


@dataclass(frozen=True, slots=True)
class SessionFileWorkspace:
    """A host-owned lookup result; unavailable results never have a root."""

    status: str
    root: Path | None = None
    cwd: Path | None = None
    project_root: Path | None = None
    message: str = ""


@dataclass
class _TreeBudget:
    nodes: int = 0
    bytes: int = 0
    truncated: bool = False
    read_failed: bool = False


def _valid_session_id(value: str) -> bool:
    return (
        isinstance(value, str) and bool(value) and len(value) <= 256
        and value not in {".", ".."}
        and not any(character in value for character in ("/", "\\", ":", "\0"))
    )


def _relative_path(value: str, *, allow_root: bool = False) -> str:
    if allow_root and value == "":
        return ""
    if (
        not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH
        or "\\" in value or ":" in value or "\0" in value
        or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("A normalized relative workspace path is required")
    return value


def _confined(root: Path, relative: str) -> Path:
    target = (root / _relative_path(relative, allow_root=True)).resolve(strict=True)
    if target != root and root not in target.parents:
        raise PermissionError("The path leaves this session's workspace")
    return target


class SessionWorkspaceFiles:
    """Resolve session roots and supply bounded directory data.

    Args:
        snapshot: Optional read-only lookup of an existing session's immutable
            runtime paths. The callback runs on the event loop; filesystem work
            runs in threads. A lookup must never create an Agent or workspace.
    """

    def __init__(
        self,
        snapshot: Callable[[str, str], RuntimeWorkspacePaths | None] | None = None,
    ) -> None:
        self._snapshot = snapshot

    async def workspace(self, session_id: str, channel_id: str = "web") -> SessionFileWorkspace:
        """Read the host's current binding without allocating any directories."""
        if not _valid_session_id(session_id):
            return SessionFileWorkspace("unavailable", message="A valid session is required")
        try:
            paths = self._snapshot(channel_id, session_id) if self._snapshot is not None else None
            return await asyncio.to_thread(self._workspace, session_id, paths)
        except FileNotFoundError:
            return SessionFileWorkspace("unavailable", message="The session workspace no longer exists")
        except (OSError, RuntimeError, ValueError):
            return SessionFileWorkspace("error", message="The session workspace could not be resolved")

    def _workspace(self, session_id: str, paths: RuntimeWorkspacePaths | None) -> SessionFileWorkspace:
        metadata = get_session_metadata(session_id, enable_writeback=False)
        mode = deprecate_mode(metadata.get("mode")) if metadata else ""
        if is_team_mode(mode) or metadata.get("team_name"):
            return SessionFileWorkspace("unsupported", message="Team workspace browsing is not supported yet")
        if metadata and not is_single_agent_mode(mode):
            return SessionFileWorkspace("unsupported", message="This session mode does not support workspace browsing")
        if not metadata and paths is None:
            return SessionFileWorkspace("unavailable", message="The session has no workspace binding yet")
        project = str(metadata.get("project_dir") or "").strip()
        if paths is not None:
            root = paths.runtime_workspace_root.resolve(strict=True)
            cwd = paths.cwd.resolve(strict=True)
            project_root = None if paths.is_projectless else root
            if project and Path(project).expanduser().resolve(strict=True) != root:
                return SessionFileWorkspace("unavailable", message="The session workspace binding has changed")
        elif project:
            candidate = Path(project).expanduser()
            if not candidate.is_absolute():
                return SessionFileWorkspace("unavailable", message="The session project binding is invalid")
            root = candidate.resolve(strict=True)
            cwd = None
            project_root = root
        else:
            registered = get_registered_projectless_task_root(session_id)
            if registered is None:
                return SessionFileWorkspace("unavailable", message="The session has no workspace binding yet")
            root = registered.resolve(strict=True)
            cwd = (root / "work").resolve(strict=True)
            project_root = None
        if not root.is_dir() or (
            cwd is not None and (not cwd.is_dir() or (cwd != root and root not in cwd.parents))
        ):
            return SessionFileWorkspace("unavailable", message="The session workspace is unavailable")
        return SessionFileWorkspace("ready", root=root, cwd=cwd, project_root=project_root)

    async def files(
        self,
        session_id: str,
        expanded_paths: list[str],
        channel_id: str = "web",
    ) -> dict[str, Any]:
        """List the root and explicitly expanded directories within tree budgets."""
        if not isinstance(expanded_paths, list) or len(expanded_paths) > MAX_EXPANDED_DIRECTORIES:
            return {"status": "error", "treeData": [], "message": "Invalid expanded directory list"}
        try:
            expanded = {_relative_path(path) for path in expanded_paths}
        except (TypeError, ValueError):
            return {"status": "error", "treeData": [], "message": "Invalid expanded directory path"}
        workspace = await self.workspace(session_id, channel_id)
        if workspace.status != "ready":
            return {"status": workspace.status, "treeData": [], "message": workspace.message}
        try:
            return await asyncio.to_thread(self._files, workspace.root, expanded)
        except (OSError, RuntimeError, ValueError):
            return {"status": "error", "treeData": [], "message": "The workspace directory could not be read"}

    def _files(self, root: Path, expanded: set[str]) -> dict[str, Any]:
        # Expanded keys are repeated in the component props, so charge their
        # serialized bytes to the same budget before adding directory nodes.
        budget = _TreeBudget(bytes=len(json.dumps(sorted(expanded), ensure_ascii=True).encode("utf-8")))
        tree, truncated = self._directory(root, "", expanded, 1, budget)
        result: dict[str, Any] = {
            "status": "ready", "treeData": tree,
            "truncated": budget.truncated or truncated, "readFailed": budget.read_failed,
        }
        if result["readFailed"]:
            result["message"] = "Some workspace directories could not be read; refresh to retry"
        elif result["truncated"]:
            result["message"] = "Only part of this directory tree is shown because the browsing limit was reached"
        return result

    def _directory(
        self,
        root: Path,
        relative: str,
        expanded: set[str],
        depth: int,
        budget: _TreeBudget,
    ) -> tuple[list[dict[str, Any]], bool]:
        directory = _confined(root, relative)
        entries: list[os.DirEntry[str]] = []
        truncated = False
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if len(entries) == MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    break
                entries.append(entry)
        entries.sort(key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.casefold(), item.name))
        nodes: list[dict[str, Any]] = []
        for entry in entries:
            path = f"{relative}/{entry.name}" if relative else entry.name
            try:
                if entry.is_symlink():
                    truncated = True
                    continue
                path = _relative_path(path)
                target = _confined(root, path)
                file_stat = target.stat()
            except (OSError, RuntimeError, ValueError):
                truncated = True
                budget.read_failed = True
                continue
            if not stat.S_ISDIR(file_stat.st_mode) and not stat.S_ISREG(file_stat.st_mode):
                truncated = True
                continue
            node: dict[str, Any] = {
                "key": path, "title": entry.name, "path": path,
                "kind": "directory" if stat.S_ISDIR(file_stat.st_mode) else "file",
            }
            node_bytes = len(json.dumps(node, ensure_ascii=True).encode("utf-8")) + 64
            if budget.nodes >= MAX_TREE_NODES or budget.bytes + node_bytes > MAX_TREE_BYTES:
                truncated = True
                break
            budget.nodes += 1
            budget.bytes += node_bytes
            if node["kind"] == "directory":
                node["loaded"] = False
                if path in expanded and depth < MAX_TREE_DEPTH:
                    try:
                        children, child_truncated = self._directory(root, path, expanded, depth + 1, budget)
                        node.update({"children": children, "loaded": True})
                        if child_truncated:
                            node["truncated"] = True
                    except (OSError, RuntimeError, ValueError):
                        node["truncated"] = True
                        budget.read_failed = True
                        truncated = True
                elif path in expanded:
                    node["truncated"] = True
                    truncated = True
            nodes.append(node)
        budget.truncated = budget.truncated or truncated
        return nodes, truncated
