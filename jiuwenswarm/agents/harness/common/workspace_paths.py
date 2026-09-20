# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared workspace path resolution and display sanitization helpers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

WORKSPACE_CURRENT_URI = "workspace://current"
WORKSPACE_CURRENT_URI_PREFIX = f"{WORKSPACE_CURRENT_URI}/"
STALE_SANDBOX_ARTIFACT_PATH = "[stale sandbox artifact path redacted]"
SanitizeVisibleMode = Literal["workspace_visible", "review_ui"]

_PATH_KEYS = frozenset(
    {
        "abs_file_path",
        "abs_file_path_list",
        "abs_file_paths",
        "cwd",
        "destination",
        "directory",
        "file",
        "file_path",
        "file_path_list",
        "file_paths",
        "filepath",
        "filename",
        "output",
        "output_path",
        "path",
        "source",
        "source_path",
        "src",
        "target_path",
        "workdir",
    }
)
_SANDBOX_ARTIFACT_PATTERN = re.compile(
    r"(?<![\w:/])"
    r"(?:~/?|/)"
    r"[^'\"<>)\],;:\s\r\n]*"
    r"\.sandbox-artifacts/[^'\"<>)\],;:\r\n]+"
    r"(?:/[^'\"<>)\],;:\r\n]*)?"
)
_REVIEW_UI_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w:/*])/(?!/)[^'\"<>)\],;:\s\r\n]+"
    r"(?:/[^'\"<>)\],;:\s\r\n]+)*"
)
_REVIEW_UI_HOME_PATH_PATTERN = re.compile(r"(?<![\w])~(?:/[^'\"<>)\],;:\s\r\n]+)+")
_REVIEW_UI_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"\b[A-Za-z]:[\\/][^'\";|&<>`,，。；、\r\n\s]+"
)


@dataclass(frozen=True)
class WorkspacePathResolution:
    """Canonical resolver result for workspace-scoped path checks."""

    original_text: str
    canonical_path: Path | None
    display_path: str
    relation: str
    rejected_reason: str = ""


def normalize_workspace_root(workspace_root: str | Path | None) -> Path | None:
    if workspace_root is None:
        return None
    return Path(workspace_root).expanduser().resolve(strict=False)


def sandbox_artifact_session_root(path: str | Path) -> Path | None:
    normalized = Path(path).expanduser().resolve(strict=False)
    parts = normalized.parts
    for index, part in enumerate(parts):
        if part == ".sandbox-artifacts" and index + 1 < len(parts):
            return Path(*parts[: index + 2])
    return None


def is_sandbox_artifact_workspace_root(path: str | Path) -> bool:
    normalized = Path(path).expanduser().resolve(strict=False)
    return sandbox_artifact_session_root(normalized) == normalized


def resolve_workspace_path(
    raw_path: str | Path,
    workspace_root: str | Path | None,
    *,
    resolve_relative: bool = True,
    reject_stale_sandbox_path: bool = True,
) -> Path | None:
    """Resolve a model-facing path into a canonical path.

    ``workspace://current/...`` and relative paths are constrained to the current
    workspace root. Absolute paths are preserved, except legacy sandbox artifact
    roots are rejected when a current workspace root is known.
    """

    path_text = _normalize_path_text(raw_path)
    if not path_text:
        return None
    root = normalize_workspace_root(workspace_root)

    if path_text == WORKSPACE_CURRENT_URI:
        path_text = WORKSPACE_CURRENT_URI_PREFIX
    if path_text.startswith(WORKSPACE_CURRENT_URI_PREFIX):
        if root is None:
            return None
        relative_text = path_text.removeprefix(WORKSPACE_CURRENT_URI_PREFIX)
        candidate = (root / relative_text).expanduser().resolve(strict=False)
        if _is_relative_to(candidate, root):
            return candidate
        return None

    raw_candidate = Path(path_text).expanduser()
    if not raw_candidate.is_absolute():
        if root is None or not resolve_relative:
            return raw_candidate.resolve(strict=False)
        candidate = (root / raw_candidate).resolve(strict=False)
        if _is_relative_to(candidate, root):
            return candidate
        return None

    resolved = raw_candidate.resolve(strict=False)
    if reject_stale_sandbox_path and root is not None:
        current_session_root = sandbox_artifact_session_root(root)
        candidate_session_root = sandbox_artifact_session_root(resolved)
        if candidate_session_root is not None and current_session_root is None:
            return None
        if (
            candidate_session_root is not None
            and candidate_session_root != current_session_root
        ):
            return None
    return resolved


def resolve_workspace_path_details(
    raw_path: str | Path,
    workspace_root: str | Path | None,
    *,
    resolve_relative: bool = True,
) -> WorkspacePathResolution:
    """Resolve a path and describe its relationship to the current workspace."""

    original_text = _normalize_path_text(raw_path)
    root = normalize_workspace_root(workspace_root)
    resolved = resolve_workspace_path(
        raw_path,
        root,
        resolve_relative=resolve_relative,
        reject_stale_sandbox_path=False,
    )
    if resolved is None:
        return WorkspacePathResolution(
            original_text=original_text,
            canonical_path=None,
            display_path=original_text,
            relation="unresolved",
            rejected_reason="workspace_path_unresolved",
        )
    relation = "external"
    if root is not None and _is_relative_to(resolved, root):
        relation = "workspace"
    return WorkspacePathResolution(
        original_text=original_text,
        canonical_path=resolved,
        display_path=display_workspace_path(resolved, root),
        relation=relation,
    )


def display_workspace_path(
    raw_path: str | Path,
    workspace_root: str | Path | None,
) -> str:
    """Return a stable model/UI-facing path.

    Current workspace paths are no longer rewritten to ``workspace://current``.
    The URI form remains accepted as input for compatibility.
    """

    path_text = _normalize_path_text(raw_path)
    if not path_text:
        return path_text
    root = normalize_workspace_root(workspace_root)
    if path_text == WORKSPACE_CURRENT_URI:
        return root.as_posix() if root is not None else WORKSPACE_CURRENT_URI_PREFIX
    if path_text.startswith(WORKSPACE_CURRENT_URI_PREFIX):
        resolved = resolve_workspace_path(
            path_text, root, reject_stale_sandbox_path=False
        )
        return resolved.as_posix() if resolved is not None else path_text

    if root is None:
        if sandbox_artifact_session_root(path_text) is not None:
            return STALE_SANDBOX_ARTIFACT_PATH
        return path_text

    path = Path(path_text).expanduser().resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError:
        if sandbox_artifact_session_root(path) is not None:
            return STALE_SANDBOX_ARTIFACT_PATH
        return path_text
    if relative.as_posix() in ("", "."):
        return root.as_posix()
    return path.as_posix()


def sanitize_visible_text(
    value: str,
    workspace_root: str | Path | None,
    *,
    mode: SanitizeVisibleMode = "workspace_visible",
    max_length: int = 60000,
) -> str:
    """Sanitize text emitted to UI/reviewer/model-visible tool messages."""

    if not value:
        return value
    if mode == "review_ui":
        return _sanitize_review_ui_text(
            value,
            workspace_root,
            max_length=max_length,
        )
    sanitized = str(value)
    return _SANDBOX_ARTIFACT_PATTERN.sub(
        lambda match: _sandbox_artifact_replacement(match.group(0)),
        sanitized,
    )


def sanitize_visible_value(
    value: Any,
    workspace_root: str | Path | None,
    *,
    mode: SanitizeVisibleMode = "workspace_visible",
    max_length: int = 60000,
) -> Any:
    if isinstance(value, str):
        return sanitize_visible_text(
            value,
            workspace_root,
            mode=mode,
            max_length=max_length,
        )
    if isinstance(value, Mapping):
        return {
            sanitize_visible_text(
                key,
                workspace_root,
                mode=mode,
                max_length=max_length,
            )
            if isinstance(key, str)
            else key: sanitize_visible_value(
                item,
                workspace_root,
                mode=mode,
                max_length=max_length,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_visible_value(
                item,
                workspace_root,
                mode=mode,
                max_length=max_length,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            sanitize_visible_value(
                item,
                workspace_root,
                mode=mode,
                max_length=max_length,
            )
            for item in value
        )
    return value


def sanitize_review_ui_value(
    value: Any,
    workspace_root: str | Path | None,
    *,
    max_length: int = 60000,
) -> Any:
    return sanitize_visible_value(
        value,
        workspace_root,
        mode="review_ui",
        max_length=max_length,
    )


def resolve_workspace_path_arguments(
    value: Any,
    workspace_root: str | Path | None,
) -> Any:
    """Resolve path-like argument fields for tools that explicitly opt in."""

    root = normalize_workspace_root(workspace_root)
    if root is None:
        return value
    return _resolve_workspace_path_arguments(value, root, current_key=None)


def _resolve_workspace_path_arguments(
    value: Any,
    workspace_root: Path,
    *,
    current_key: str | None,
) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _resolve_workspace_path_arguments(
                item,
                workspace_root,
                current_key=str(key).lower(),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_workspace_path_arguments(
                item,
                workspace_root,
                current_key=current_key,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _resolve_workspace_path_arguments(
                item,
                workspace_root,
                current_key=current_key,
            )
            for item in value
        )
    if isinstance(value, str) and current_key in _PATH_KEYS:
        resolved = resolve_workspace_path(value, workspace_root)
        if resolved is None:
            raise ValueError(f"Path is outside current workspace or stale: {value}")
        return resolved.as_posix()
    return value


def _normalize_path_text(raw_path: str | Path) -> str:
    return unquote(unicodedata.normalize("NFKC", str(raw_path or "").strip())).replace(
        "\\",
        "/",
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sandbox_artifact_replacement(matched: str) -> str:
    if matched.startswith(WORKSPACE_CURRENT_URI):
        return matched
    return STALE_SANDBOX_ARTIFACT_PATH


def _sanitize_review_ui_text(
    value: str,
    workspace_root: str | Path | None,
    *,
    max_length: int,
) -> str:
    sanitized = _relativize_workspace_root_prefixes(str(value), workspace_root)
    sanitized = _REVIEW_UI_HOME_PATH_PATTERN.sub(
        lambda match: _review_ui_path_replacement(match.group(0), workspace_root),
        sanitized,
    )
    sanitized = _REVIEW_UI_WINDOWS_ABSOLUTE_PATH_PATTERN.sub(
        lambda match: _review_ui_path_replacement(match.group(0), workspace_root),
        sanitized,
    )
    sanitized = _REVIEW_UI_POSIX_ABSOLUTE_PATH_PATTERN.sub(
        lambda match: _review_ui_path_replacement(match.group(0), workspace_root),
        sanitized,
    )
    return _redact_review_ui_secrets(sanitized, max_length=max_length)


def _relativize_workspace_root_prefixes(
    value: str,
    workspace_root: str | Path | None,
) -> str:
    root = normalize_workspace_root(workspace_root)
    if root is None:
        return value

    root_forms = {root.as_posix(), str(root)}
    home = Path.home().expanduser().resolve(strict=False)
    try:
        relative_to_home = root.relative_to(home)
    except ValueError:
        pass
    else:
        root_forms.add(f"~/{relative_to_home.as_posix()}")

    sanitized = value
    for root_form in sorted(root_forms, key=len, reverse=True):
        if not root_form:
            continue
        root_pattern = re.escape(root_form)
        traversal_starts = _workspace_root_traversal_starts(sanitized, root_form)
        sanitized = re.sub(
            rf"(?<![\w./:-]){root_pattern}"
            rf"(?![\\/]\.\.(?:[\\/]|$))[\\/]",
            lambda match: match.group(0) if match.start() in traversal_starts else "",
            sanitized,
        )
        sanitized = re.sub(
            rf"(?<![\w./:-]){root_pattern}(?=$|[\s'\"<>)\],;:])",
            ".",
            sanitized,
        )
    return sanitized


def _workspace_root_traversal_starts(value: str, root_form: str) -> set[int]:
    """Return current-workspace root offsets whose path contains ``..``.

    This intentionally uses bounded token scanning rather than a nested regex:
    tool payloads are agent-controlled and must not make review rendering incur
    unbounded regex backtracking.
    """

    starts: set[int] = set()
    search_from = 0
    while True:
        start = value.find(root_form, search_from)
        if start < 0:
            return starts
        search_from = start + 1
        if start and _is_local_path_prefix_character(value[start - 1]):
            continue
        if _path_from_root_has_parent_segment(value, start + len(root_form)):
            starts.add(start)


def _path_from_root_has_parent_segment(value: str, root_end: int) -> bool:
    """Scan slash-delimited path segments after a root without backtracking."""

    position = root_end
    value_length = len(value)
    while position < value_length and value[position] in "/\\":
        position += 1
        segment_start = position
        while position < value_length:
            current = value[position]
            if current in "/\\":
                break
            if current in "'\";|&<>`,，。；、\r\n":
                return False
            position += 1
        if value[segment_start:position] == "..":
            return True
    return False


def _is_local_path_prefix_character(character: str) -> bool:
    return character.isalnum() or character == "_" or character in "./:-"


def _review_ui_path_replacement(
    matched: str,
    workspace_root: str | Path | None,
) -> str:
    if not matched:
        return matched
    root = normalize_workspace_root(workspace_root)
    try:
        path = Path(matched).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return matched
    if root is None:
        return matched
    try:
        relative = path.relative_to(root)
    except ValueError:
        return matched
    relative_text = relative.as_posix()
    return "." if relative_text in ("", ".") else relative_text


def _redact_review_ui_secrets(value: str, *, max_length: int) -> str:
    from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
        redact_secret_values_preserve_format,
    )

    return redact_secret_values_preserve_format(value, max_length=max_length)
