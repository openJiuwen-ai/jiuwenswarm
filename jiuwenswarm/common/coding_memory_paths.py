# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for resolving project-scoped coding memory paths."""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import unicodedata
from os import PathLike

DEFAULT_CODING_MEMORY_PROJECT = "default"
CODING_MEMORY_PROJECT_HASH_LENGTH = 12
CODING_MEMORY_PROJECT_NAME_MAX_BYTES = 80

_INVALID_DIRECTORY_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _normalize_coding_memory_project_path(
    project_dir: str | PathLike[str],
) -> str:
    """Return the canonical path used to isolate a project's coding memory."""
    expanded = os.path.expanduser(str(project_dir).strip())
    canonical = os.path.realpath(os.path.abspath(expanded))
    canonical = os.path.normcase(os.path.normpath(canonical))
    return unicodedata.normalize("NFC", canonical)


def _sanitize_coding_memory_project_name(project_name: str) -> str:
    """Return a readable, cross-platform-safe storage-directory prefix."""
    sanitized = _INVALID_DIRECTORY_CHARS.sub("_", project_name)
    sanitized = sanitized.strip().rstrip(". ")
    encoded = sanitized.encode("utf-8", errors="surrogatepass")
    if len(encoded) <= CODING_MEMORY_PROJECT_NAME_MAX_BYTES:
        return sanitized
    return encoded[:CODING_MEMORY_PROJECT_NAME_MAX_BYTES].decode(
        "utf-8", errors="ignore"
    )


def resolve_coding_memory_project_name(project_dir: str | PathLike[str] | None) -> str:
    """Return the path-isolated directory key used under ``coding_memory/``.

    The readable basename alone is not a sufficient project identity: unrelated
    workspaces commonly share names such as ``frontend`` or ``project``.  A hash
    of the canonical absolute path keeps repeated sessions for the same project
    together while isolating same-named projects at different locations.
    """
    if project_dir is None:
        return DEFAULT_CODING_MEMORY_PROJECT

    raw_project_dir = str(project_dir).strip()
    if not raw_project_dir:
        return DEFAULT_CODING_MEMORY_PROJECT

    canonical_path = _normalize_coding_memory_project_path(raw_project_dir)
    project_name = ntpath.basename(canonical_path.rstrip("/\\"))
    project_name = _sanitize_coding_memory_project_name(project_name)
    if not project_name:
        project_name = "project"

    path_hash = hashlib.sha256(
        canonical_path.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:CODING_MEMORY_PROJECT_HASH_LENGTH]
    return f"{project_name}-{path_hash}"


def resolve_project_coding_memory_dir(
    *,
    agent_workspace_dir: str | PathLike[str],
    project_dir: str | PathLike[str] | None,
) -> str:
    """Resolve ``<agent_workspace>/coding_memory/<project_name>-<path_hash>``."""
    return os.path.join(
        os.path.abspath(str(agent_workspace_dir)),
        "coding_memory",
        resolve_coding_memory_project_name(project_dir),
    )
