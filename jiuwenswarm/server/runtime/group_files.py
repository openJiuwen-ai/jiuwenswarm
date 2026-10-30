"""Per-group overrides for published file attachments (rename and delete).

Group history and task snapshots are append-only records, so file changes are
stored beside them in ``group.json`` under ``file_overrides`` and applied when
attachments are returned to clients. Renaming only changes the displayed name;
deleting hides the attachment and removes the file from disk when it lives in
the group's own team workspace.
"""

from __future__ import annotations

import copy
import logging
import time
from pathlib import Path

from openjiuwen.agent_teams.paths import team_home

logger = logging.getLogger(__name__)

FILE_NAME_MAX = 255
FILE_PATH_MAX = 4096


def _deleted(path: str, override: dict) -> bool:
    """A deleted path stays hidden unless a newer file was written to it afterwards."""
    deleted_at = override.get("deleted_at")
    if not isinstance(deleted_at, (int, float)):
        return False
    try:
        return Path(path).stat().st_mtime <= deleted_at
    except OSError:
        return True


def normalize_attachment(item: dict) -> dict:
    """Adapt agent file fields to the public group attachment contract."""
    result = dict(item)
    for target, source in (("path", "file_path"), ("name", "file_name")):
        if not result.get(target) and isinstance(result.get(source), str):
            result[target] = result[source]
    return result


def apply_file_overrides(attachments: list, overrides: dict) -> list:
    """Return attachments with renamed display names and deleted files removed."""
    if not isinstance(attachments, list):
        return attachments
    result = []
    for item in attachments:
        if isinstance(item, dict):
            item = normalize_attachment(item)
        path = item.get("path") if isinstance(item, dict) else None
        override = overrides.get(path) if isinstance(path, str) else None
        if not override:
            result.append(item)
            continue
        if _deleted(path, override):
            continue
        if override.get("name"):
            item = {**item, "name": override["name"]}
        result.append(item)
    return result


def validate_file_name(value) -> str:
    """Accept a single file name: no separators, no dot entries, no control characters."""
    if not isinstance(value, str):
        raise ValueError("invalid name")
    name = value.strip()
    if (
        not name
        or len(name) > FILE_NAME_MAX
        or name in {".", ".."}
        or any(char in name for char in "/\\")
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("invalid name")
    return name


def validate_file_path(value) -> str:
    if not isinstance(value, str) or not value or len(value) > FILE_PATH_MAX:
        raise ValueError("invalid path")
    return value


def group_attachment_paths(messages: list, executions: list) -> set[str]:
    paths = set()
    for record in [*messages, *executions]:
        for item in apply_file_overrides(record.get("attachments") or [], {}):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.add(item["path"])
    return paths


def record_rename(state: dict, path: str, name: str) -> dict:
    overrides = state.setdefault("file_overrides", {})
    overrides[path] = {"name": name, "updated_at": time.time()}
    return copy.deepcopy(overrides)


def record_delete(state: dict, path: str) -> dict:
    overrides = state.setdefault("file_overrides", {})
    overrides[path] = {**overrides.get(path, {}), "deleted_at": time.time()}
    return copy.deepcopy(overrides)


def remove_workspace_file(team_name: str, path: str) -> bool:
    """Unlink a regular file inside the group's team workspace; anything else is left on disk."""
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return False
        root = team_home(team_name).resolve()
        if not candidate.resolve().is_relative_to(root):
            return False
        candidate.unlink()
        return True
    except OSError:
        logger.warning("group file could not be removed from disk: %s", path, exc_info=True)
        return False
