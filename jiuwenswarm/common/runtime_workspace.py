"""Resolve internal and user-operable workspaces for single-agent modes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.common.projectless_workspace import (
    get_projectless_task_workspace,
)


@dataclass(frozen=True, slots=True)
class RuntimeWorkspacePaths:
    """Distinct paths used by one Agent or Code request."""

    internal_workspace_dir: Path
    runtime_workspace_root: Path
    cwd: Path
    project_root: Path
    work_dir: Path | None = None
    outputs_dir: Path | None = None
    is_projectless: bool = False


def resolve_runtime_workspace_paths(
    *,
    internal_workspace_dir: str | Path,
    project_dir: str | None,
    workspace_dir: str | None,
    cwd: str | None,
    session_id: str | None,
    task_name: str | None,
    bind_request: bool,
) -> RuntimeWorkspacePaths:
    """Resolve one request without treating a bare ``cwd`` as a project.

    ``project_dir`` is the canonical project identity. ``workspace_dir`` is a
    compatibility-level explicit workspace override. A standalone ``cwd`` is
    only accepted when it is an existing directory inside that explicit root.
    """

    internal_root = _resolved_path(internal_workspace_dir)
    explicit_root = _optional_path(project_dir) or _optional_path(workspace_dir)
    if explicit_root is not None:
        runtime_cwd = _cwd_inside_root(cwd, explicit_root) or explicit_root
        return RuntimeWorkspacePaths(
            internal_workspace_dir=internal_root,
            runtime_workspace_root=explicit_root,
            cwd=runtime_cwd,
            project_root=explicit_root,
        )

    if not bind_request:
        return RuntimeWorkspacePaths(
            internal_workspace_dir=internal_root,
            runtime_workspace_root=internal_root,
            cwd=internal_root,
            project_root=internal_root,
        )

    task_workspace = get_projectless_task_workspace(session_id, task_name)
    return RuntimeWorkspacePaths(
        internal_workspace_dir=internal_root,
        runtime_workspace_root=task_workspace.root_dir,
        cwd=task_workspace.work_dir,
        project_root=task_workspace.root_dir,
        work_dir=task_workspace.work_dir,
        outputs_dir=task_workspace.outputs_dir,
        is_projectless=True,
    )


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _optional_path(value: str | None) -> Path | None:
    raw = str(value or "").strip()
    return _resolved_path(raw) if raw else None


def _cwd_inside_root(cwd: str | None, root: Path) -> Path | None:
    candidate = _optional_path(cwd)
    if candidate is None or not candidate.is_dir():
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


__all__ = ["RuntimeWorkspacePaths", "resolve_runtime_workspace_paths"]
