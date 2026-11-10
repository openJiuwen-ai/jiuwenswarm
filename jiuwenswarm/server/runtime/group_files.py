"""Per-group overrides for published file attachments (rename and delete).

Group history and task snapshots are append-only records, so file changes are
stored beside them in ``group.json`` under ``file_overrides`` and applied when
attachments are returned to clients. Renaming moves the file and remaps historical attachment paths;
deleting hides the attachment and removes the file from disk when it lives in
the group's own team workspace.
"""

from __future__ import annotations

import copy
import logging
import os
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


def attachment_record_time(record: dict) -> float | None:
    """Use publication time, tolerating legacy execution records and millisecond messages."""
    value = (
        record.get("attachments_published_at") or record.get("timestamp")
        or record.get("updated_at") or record.get("created_at")
    )
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return value / 1000 if value > 1e12 else value


def _versions(override: dict) -> list[dict]:
    return [*override.get("previous", []), override]


def _attachment_override(path: str, overrides: dict, published_at: float | None) -> dict:
    override = overrides.get(path, {})
    if published_at is None:
        return override
    for version in _versions(override):
        cutoff = version.get("applies_before", version.get("updated_at", version.get("deleted_at")))
        if cutoff is None or published_at <= cutoff:
            return version
    return {}


def apply_file_overrides(attachments: list, overrides: dict, published_at: float | None = None) -> list:
    """Return current attachment names and paths, with deleted files removed."""
    if not isinstance(attachments, list):
        return attachments
    result = []
    for item in attachments:
        if isinstance(item, dict):
            item = normalize_attachment(item)
        path = item.get("path") if isinstance(item, dict) else None
        override = _attachment_override(path, overrides, published_at) if isinstance(path, str) else None
        if not override:
            result.append(item)
            continue
        current_path = override.get("path") or path
        if override.get("deleted_at") and (published_at is not None or _deleted(current_path, override)):
            continue
        if override.get("name"):
            item = {**item, "name": override["name"]}
        if current_path != path:
            item = {**item, "path": current_path}
            if "file_path" in item:
                item["file_path"] = current_path
            for key in ("url", "download_url", "download_token"):
                item.pop(key, None)
        result.append(item)
    return result


def validate_file_name(value) -> str:
    """Accept a single file name: no separators, no dot entries, no control characters."""
    if not isinstance(value, str):
        raise ValueError("invalid name")
    name = value.strip()
    invalid_name = not name or len(name) > FILE_NAME_MAX or name in {".", ".."}
    if invalid_name or any(char in "/\\" or ord(char) < 32 for char in name):
        raise ValueError("invalid name")
    return name


def validate_file_path(value) -> str:
    if not isinstance(value, str) or not value or len(value) > FILE_PATH_MAX:
        raise ValueError("invalid path")
    return value


def group_attachment_paths(messages: list, executions: list, overrides: dict | None = None) -> set[str]:
    paths = set()
    for record in [*messages, *executions]:
        for item in apply_file_overrides(record.get("attachments") or [], {}):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                path = item["path"]
                override = _attachment_override(path, overrides or {}, attachment_record_time(record))
                # Deleted attachments still belong to the group, so delete retries stay idempotent.
                paths.add(override.get("path") or path)
    return paths


def _record_override(overrides: dict, path: str, replacement: dict) -> None:
    current = overrides.get(path, {})
    previous = list(current.get("previous", []))
    # Reusing a path starts a new generation without discarding older attachments' mapping.
    if current and (current.get("deleted_at") or current.get("path", path) != path):
        previous.append({key: value for key, value in current.items() if key != "previous"})
    if previous:
        replacement["previous"] = previous
    overrides[path] = replacement


def record_rename(state: dict, path: str, name: str, new_path: str) -> dict:
    overrides = state.setdefault("file_overrides", {})
    now = time.time()
    _record_override(overrides, path, {
        "name": name, "path": new_path, "updated_at": now, "applies_before": now,
    })
    for original, override in overrides.items():
        for version in _versions(override):
            if version.get("deleted_at") or version.get("path", original) != path:
                continue
            version.setdefault("applies_before", version.get("updated_at", now))
            version.update(name=name, path=new_path)
    return copy.deepcopy(overrides)


def record_delete(state: dict, path: str) -> dict:
    overrides = state.setdefault("file_overrides", {})
    now = time.time()
    _record_override(overrides, path, {"deleted_at": now, "applies_before": now})
    for original, override in overrides.items():
        for version in _versions(override):
            if version.get("deleted_at") or version.get("path", original) != path:
                continue
            version.setdefault("applies_before", version.get("updated_at", now))
            version["deleted_at"] = now
    return copy.deepcopy(overrides)


def move_file(source: Path, target: Path) -> None:
    """Move within a directory without overwriting an existing destination on any platform."""
    os.link(source, target, follow_symlinks=False)
    try:
        source.unlink()
    except OSError:
        target.unlink()
        raise


def rename_workspace_file(team_name: str, path: str, name: str) -> Path:
    """Rename an owned regular file, refusing symlinks, outside paths and collisions."""
    source = Path(path)
    root = team_home(team_name).resolve()
    if source.is_symlink() or not source.resolve().is_relative_to(root):
        raise ValueError("Only regular files inside this group's workspace can be renamed")
    if not source.is_file():
        raise FileNotFoundError("The file no longer exists or is not a regular file")
    target = source.with_name(name)
    if source != target:
        move_file(source, target)
    return target


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
