# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only, transport-neutral Memory inspection contracts.

The Runtime API deliberately exposes metadata and configured state only.  It
does not start the Memory manager, build an index, modify configuration, open
an editor, or launch a file explorer.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class MemoryCatalogError(ValueError):
    """Stable Runtime failure with no transport-specific representation."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MemoryScopeInput:
    """Local authority used to resolve one Memory inspection scope."""

    channel_id: str
    session_id: str | None = None
    mode: str = "agent.code.normal"
    project_dir: str = ""
    trusted_dirs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemorySource:
    path: str
    relative_path: str
    kind: str
    size: int
    mtime: float
    exists: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MemoryListResult:
    files: tuple[MemorySource, ...]
    mode: str
    project_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [item.to_dict() for item in self.files],
            "mode": self.mode,
            "project_dir": self.project_dir,
        }


@dataclass(frozen=True, slots=True)
class MemoryStatusResult:
    current_mode: str
    storage_mode: str
    engine: str
    enabled: bool
    proactive: bool
    forbidden_enabled: bool
    auto_memory_enabled: bool
    auto_coding_memory: bool | None
    index_inspected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MemoryLocationsResult:
    agent_memory_dir: str
    project_dir: str
    project_memory_dir: str
    coding_memory_dir: str
    user_memory_dir: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResolvedMemoryScope:
    channel_id: str
    session_id: str
    mode: str
    project_dir: Path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_existing_directory(value: str, *, field_name: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise MemoryCatalogError(f"{field_name} is required")
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MemoryCatalogError(
            f"{field_name} does not exist",
            code="NOT_FOUND",
        ) from exc
    if not path.is_dir():
        raise MemoryCatalogError(f"{field_name} must be a directory")
    return path


def ensure_trusted_project(project_dir: Path, trusted_dirs: tuple[str, ...]) -> None:
    roots: list[Path] = []
    for value in trusted_dirs:
        if not str(value or "").strip():
            continue
        try:
            roots.append(
                resolve_existing_directory(value, field_name="trusted directory")
            )
        except MemoryCatalogError:
            # A stale optional trusted-directory entry must not make an otherwise
            # valid, explicitly trusted project unusable.  Only existing roots
            # can grant authority.
            continue
    if not roots or not any(_is_relative_to(project_dir, root) for root in roots):
        raise MemoryCatalogError("project directory is not trusted", code="FORBIDDEN")


def _safe_source(
    path: Path,
    *,
    root: Path,
    relative_root: Path,
    kind: str,
) -> MemorySource | None:
    try:
        resolved = path.resolve(strict=True)
        allowed_root = root.resolve(strict=True)
        if not resolved.is_file() or not _is_relative_to(resolved, allowed_root):
            return None
        stat = resolved.stat()
        try:
            relative_path = str(resolved.relative_to(relative_root))
        except ValueError:
            relative_path = str(resolved)
        return MemorySource(
            path=str(resolved),
            relative_path=relative_path,
            kind=kind,
            size=stat.st_size,
            mtime=stat.st_mtime,
        )
    except (OSError, RuntimeError):
        return None


def _collect_pattern(
    output: list[MemorySource],
    seen: set[str],
    *,
    root: Path,
    relative_root: Path,
    pattern: str,
    kind: str,
) -> None:
    try:
        candidates = sorted(root.glob(pattern), key=lambda item: str(item).lower())
    except OSError:
        return
    for candidate in candidates:
        source = _safe_source(
            candidate,
            root=root,
            relative_root=relative_root,
            kind=kind,
        )
        if source is None:
            continue
        normalized = os.path.normcase(source.path)
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(source)


def list_memory_sources(scope: ResolvedMemoryScope) -> MemoryListResult:
    """Return known Memory file metadata without opening Memory files."""
    from jiuwenswarm.common.coding_memory_paths import (
        resolve_project_coding_memory_dir,
    )
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    agent_workspace = get_agent_workspace_dir().expanduser().resolve()
    coding_dir = Path(
        resolve_project_coding_memory_dir(
            agent_workspace_dir=agent_workspace,
            project_dir=scope.project_dir,
        )
    )
    user_memory_dir = (Path.home() / ".jiuwen").resolve()
    files: list[MemorySource] = []
    seen: set[str] = set()

    for pattern, kind in (
        ("JIUWENSWARM.md", "project"),
        ("JIUWENSWARM.local.md", "local"),
        (".jiuwen/JIUWENSWARM.md", "project"),
        (".jiuwen/rules/*.md", "project"),
    ):
        _collect_pattern(
            files,
            seen,
            root=scope.project_dir,
            relative_root=scope.project_dir,
            pattern=pattern,
            kind=kind,
        )
    for root, pattern, kind in (
        (agent_workspace / "memory", "*.md", "auto"),
        (coding_dir, "*.md", "coding"),
        (user_memory_dir, "JIUWENSWARM.md", "user"),
        (user_memory_dir, "rules/*.md", "user"),
    ):
        _collect_pattern(
            files,
            seen,
            root=root,
            relative_root=root,
            pattern=pattern,
            kind=kind,
        )
    if os.name != "nt":
        managed_root = Path("/etc/jiuwen")
        for pattern in ("JIUWENSWARM.md", "rules/*.md"):
            _collect_pattern(
                files,
                seen,
                root=managed_root,
                relative_root=managed_root,
                pattern=pattern,
                kind="managed",
            )
    return MemoryListResult(
        files=tuple(files),
        mode=scope.mode,
        project_dir=str(scope.project_dir),
    )


def get_memory_status(scope: ResolvedMemoryScope) -> MemoryStatusResult:
    """Read configuration only; do not initialize Memory runtime resources."""
    from jiuwenswarm.agents.harness.common.memory import is_memory_enabled
    from jiuwenswarm.agents.harness.common.memory.config import (
        is_auto_memory_enabled,
        is_proactive_memory,
    )
    from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
        get_memory_engine,
    )
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.mode_matrix import is_code_profile_mode

    config = get_config()
    memory_config = config.get("memory") if isinstance(config, dict) else {}
    memory_config = memory_config if isinstance(memory_config, dict) else {}
    forbidden = memory_config.get("forbidden_memory_definition")
    forbidden = forbidden if isinstance(forbidden, dict) else {}
    is_code = is_code_profile_mode(scope.mode) or scope.mode.startswith("code")
    auto_memory = is_auto_memory_enabled(
        "code" if is_code else scope.mode,
        config,
    )
    return MemoryStatusResult(
        current_mode=scope.mode,
        storage_mode=str(memory_config.get("mode") or "local"),
        engine=get_memory_engine(config),
        enabled=is_memory_enabled(scope.mode, config),
        proactive=is_proactive_memory(scope.mode, config),
        forbidden_enabled=bool(forbidden.get("enabled", False)),
        auto_memory_enabled=auto_memory,
        auto_coding_memory=auto_memory if is_code else None,
    )


def get_memory_locations(scope: ResolvedMemoryScope) -> MemoryLocationsResult:
    """Return paths only; callers decide whether and how to display them."""
    from jiuwenswarm.common.coding_memory_paths import (
        resolve_project_coding_memory_dir,
    )
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    agent_workspace = get_agent_workspace_dir().expanduser().resolve()
    return MemoryLocationsResult(
        agent_memory_dir=str(agent_workspace / "memory"),
        project_dir=str(scope.project_dir),
        project_memory_dir=str(scope.project_dir),
        coding_memory_dir=resolve_project_coding_memory_dir(
            agent_workspace_dir=agent_workspace,
            project_dir=scope.project_dir,
        ),
        user_memory_dir=str((Path.home() / ".jiuwen").resolve()),
    )


__all__ = [
    "MemoryCatalogError",
    "MemoryListResult",
    "MemoryLocationsResult",
    "MemoryScopeInput",
    "MemorySource",
    "MemoryStatusResult",
    "ResolvedMemoryScope",
    "ensure_trusted_project",
    "get_memory_locations",
    "get_memory_status",
    "list_memory_sources",
    "resolve_existing_directory",
]
