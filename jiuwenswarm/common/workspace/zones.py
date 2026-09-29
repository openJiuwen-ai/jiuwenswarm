# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作区路径 zone（相对租户根，最长前缀匹配）。"""

from __future__ import annotations

from enum import Enum
from typing import Final


class WorkspaceZone(str, Enum):
    SESSION_WORKSPACE = "session_workspace"
    SESSION_TODO = "session_todo"
    SESSION_META = "session_meta"
    SKILLS = "skills"
    ROLE_MD = "role_md"
    MEMORY = "memory"
    RUNTIME = "runtime"
    ARTIFACT = "artifact"
    OTHER = "other"


DELETABLE_ZONES: Final[frozenset[WorkspaceZone]] = frozenset(
    {
        WorkspaceZone.SESSION_WORKSPACE,
        WorkspaceZone.SESSION_TODO,
        WorkspaceZone.ARTIFACT,
    }
)

_ROLE_MD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "AGENT.md",
        "SOUL.md",
        "IDENTITY.md",
        "HEARTBEAT.md",
        "USER.md",
    }
)

_PREFIX_RULES: Final[tuple[tuple[str, WorkspaceZone], ...]] = (
    ("agent/jiuwenclaw_workspace/projects/", WorkspaceZone.SESSION_WORKSPACE),
    ("agent/jiuwenclaw_workspace/todo/", WorkspaceZone.SESSION_TODO),
    ("agent/sessions/", WorkspaceZone.SESSION_META),
    ("agent/jiuwenclaw_workspace/skills/", WorkspaceZone.SKILLS),
    ("agent/jiuwenclaw_workspace/memory/", WorkspaceZone.MEMORY),
    ("agent/jiuwenclaw_workspace/coding_memory/", WorkspaceZone.MEMORY),
    ("agent/jiuwenclaw_workspace/context/", WorkspaceZone.MEMORY),
    ("agent/jiuwenclaw_workspace/agents/", WorkspaceZone.RUNTIME),
    ("agent/jiuwenclaw_workspace/extensions/", WorkspaceZone.RUNTIME),
    ("agent/jiuwenclaw_workspace/messages/", WorkspaceZone.RUNTIME),
    ("agent/jiuwenclaw_workspace/prompt_attachment/", WorkspaceZone.ARTIFACT),
    ("agent/jiuwenclaw_workspace/received_files/", WorkspaceZone.ARTIFACT),
)


def normalize_relative_path(relative_path: str | None) -> str:
    """Normalize to posix relative path without leading ``./`` or trailing ``/`` (except root)."""
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw or raw in {".", "./"}:
        return ""
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("path_traversal")
        parts.append(part)
    return "/".join(parts)


def classify_zone(relative_path: str | None) -> WorkspaceZone:
    """Classify a path relative to the tenant workspace root."""
    path = normalize_relative_path(relative_path)
    if not path:
        return WorkspaceZone.OTHER

    name = path.rsplit("/", 1)[-1]
    parent = path[: -len(name)].rstrip("/") if "/" in path else ""
    if name in _ROLE_MD_NAMES and parent in {
        "agent/jiuwenclaw_workspace",
        "agent/workspace",
    }:
        return WorkspaceZone.ROLE_MD

    best: WorkspaceZone | None = None
    best_len = -1
    for prefix, zone in _PREFIX_RULES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            if len(prefix) > best_len:
                best = zone
                best_len = len(prefix)
    return best if best is not None else WorkspaceZone.OTHER


def is_deletable(relative_path: str | None) -> bool:
    return classify_zone(relative_path) in DELETABLE_ZONES
