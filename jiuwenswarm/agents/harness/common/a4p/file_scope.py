# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Path constraints for A4P native read and discovery tools.

Require absolute paths on both sides of the authorization boundary: the
permission host and an isolated tool worker need not share a current directory.
These checks supplement token verification; they are not a filesystem sandbox.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from a4p.intent.scope import params_match_intent_scope


READ_PATH_FIELDS = {
    "read_file": "file_path",
    "list_files": "path",
    "glob": "path",
    "grep": "path",
}
_GLOB_CHARS = frozenset("*?[")


def normalize_read_path_params(
    action: str, params: dict[str, Any], *, scope: bool = False,
) -> dict[str, Any]:
    """Validate and canonicalize an explicit native path, preserving other args."""
    field = READ_PATH_FIELDS.get(action)
    if field is None:
        return dict(params)
    raw = params.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{action} requires an explicit absolute {field}")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{action}.{field} must be absolute and contain no '..' segments")
    has_glob = any(char in raw for char in _GLOB_CHARS)
    if has_glob:
        if not (scope and action == "read_file") or any(
            char in str(path.parent) for char in _GLOB_CHARS
        ):
            raise ValueError(f"{action}.{field} only permits a filename glob for read_file authorization")
        canonical = path.parent.resolve() / path.name
    else:
        canonical = path.resolve()
    if action == "read_file" and (raw.endswith(("/", "\\")) or canonical.is_dir()):
        raise ValueError("read_file.file_path must identify a file, not a directory")
    if action in {"list_files", "glob"} and canonical.exists() and not canonical.is_dir():
        raise ValueError(f"{action}.path must identify a directory")
    result = dict(params)
    result[field] = str(canonical)
    return result


def _search_stays_inside(root: Path) -> bool:
    """Fail closed for outward links, including grep's recursive fallback.

    Do not cache this result: directory contents can change between cron runs.
    A filesystem sandbox is still needed against concurrent link replacement.
    """
    if not root.is_dir():
        return True

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in dirs + files:
            entry = Path(directory) / name
            if entry.is_symlink() and not entry.resolve().is_relative_to(root):
                return False
    return True


def read_scope_matches(
    token: dict[str, Any], action: str, params: dict[str, Any],
) -> bool:
    """Apply filesystem semantics in addition to the SDK's string glob match."""
    field = READ_PATH_FIELDS.get(action)
    if field is None:
        return True
    actual = Path(params[field])
    actions = (token.get("intent") or {}).get("actions") or []
    matches = False
    for item in actions:
        if item.get("name") != action:
            continue
        constraint = (item.get("params") or {}).get(field)
        if not isinstance(constraint, str):
            continue
        approved = Path(constraint)
        # fnmatch '*' matches '/' in the SDK. Restrict read globs to one parent.
        if action == "read_file":
            matches = actual.parent == approved.parent and fnmatch.fnmatchcase(actual.name, approved.name)
        else:
            matches = str(actual) == constraint
        # The filesystem and SDK constraints must match the SAME action.
        # Check a scope projection only after signature verification; never
        # rewrite the signed token to perform this supplementary comparison.
        if matches:
            matches, _ = params_match_intent_scope({"actions": [item]}, action=action, params=params)
        if matches:
            break
    if not matches:
        return False
    if action == "glob":
        pattern = params.get("pattern")
        # GlobTool expands braces before rglob; reject traversal in any branch.
        if not isinstance(pattern, str) or not pattern:
            return False
        if ".." in pattern or "\\" in pattern:
            return False
        if any(part.startswith("/") or ":" in part for part in pattern.replace("{", ",").split(",")):
            return False
    return action not in {"glob", "grep"} or _search_stays_inside(actual)
