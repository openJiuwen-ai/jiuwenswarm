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
    ("agent/workspace/projects/", WorkspaceZone.SESSION_WORKSPACE),
    ("agent/workspace/todo/", WorkspaceZone.SESSION_TODO),
    ("agent/sessions/", WorkspaceZone.SESSION_META),
    ("agent/workspace/skills/", WorkspaceZone.SKILLS),
    ("agent/workspace/memory/", WorkspaceZone.MEMORY),
    ("agent/workspace/coding_memory/", WorkspaceZone.MEMORY),
    ("agent/workspace/context/", WorkspaceZone.MEMORY),
    ("agent/workspace/agents/", WorkspaceZone.RUNTIME),
    ("agent/workspace/extensions/", WorkspaceZone.RUNTIME),
    ("agent/workspace/messages/", WorkspaceZone.RUNTIME),
    ("agent/workspace/prompt_attachment/", WorkspaceZone.ARTIFACT),
    ("agent/workspace/received_files/", WorkspaceZone.ARTIFACT),
    ("agent/workspace/uploads/", WorkspaceZone.ARTIFACT),
)


def _is_session_chat_uploads(path: str) -> bool:
    """``agent/sessions/<sid>/uploads`` 及其子路径：聊天附件，可删（artifact）。

    ``sid`` 为单段目录名；``history.jsonl`` / ``metadata.json`` 等会话元数据
    仍落在 ``session_meta``，不可经本规则删除。
    """
    parts = path.split("/")
    # agent / sessions / <sid> / uploads [/ ...]
    if len(parts) < 4:
        return False
    if parts[0] != "agent" or parts[1] != "sessions" or parts[3] != "uploads":
        return False
    sid = parts[2]
    return bool(sid) and sid not in {".", ".."} and "/" not in sid


def normalize_relative_path(relative_path: str | None) -> str:
    """Normalize to posix relative path without leading ``./`` or trailing ``/`` (except root).

    Rejects ``..``, NUL, and absolute forms (POSIX ``/…``, UNC ``//…``, Windows ``C:/…``).
    """
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw or raw in {".", "./"}:
        return ""
    if "\x00" in raw:
        raise ValueError("path_traversal")
    # Absolute / drive-qualified paths must not be silently coerced into relative ones.
    if raw.startswith("/"):
        raise ValueError("absolute_path")
    if len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":":
        raise ValueError("absolute_path")
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
    if name in _ROLE_MD_NAMES and parent == "agent/workspace":
        return WorkspaceZone.ROLE_MD

    # 比 ``agent/sessions/`` 更具体：会话聊天附件归 artifact（可删）。
    if _is_session_chat_uploads(path):
        return WorkspaceZone.ARTIFACT

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
