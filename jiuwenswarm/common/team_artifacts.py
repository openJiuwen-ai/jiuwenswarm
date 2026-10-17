# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team-scoped final-deliverables and per-member work directories.

A team member without a project directory still needs stable places: a
*per-member* temporary working directory (cwd, where intermediate files land
in isolation) and a *shared* final-deliverables directory (where the whole
team's outputs collect, distinguished by filename).

The on-disk layout — dated ``chat-<n>`` directories, a ``.session_id`` collision
marker and a ``metadata.json`` sidecar — is allocated by the single-agent
projectless allocator (:mod:`jiuwenswarm.common.projectless_workspace`). This
module only fixes the *root*: instead of ``<Documents>/JiuwenSwarm`` the team's
artifacts live inside the team workspace itself, so
``TeamWorkspaceRail`` auto-commit and the ``workspace_meta`` tool's git history
work on deliverables without extra plumbing::

    <team-workspace>/artifacts/<YYYY-MM-DD>/chat-<n>/
        work/<member_slug>/      # per-member temporary working directory (cwd)
        outputs/                 # shared final deliverables (by filename)

All members of one team share one ``chat-<n>`` directory. ``outputs/`` is
shared (one directory, filename-distinguished); ``work/`` is per-member so
each member's intermediate files, caches and scratch scripts are isolated and
do not pile up together — a member's cwd is its own ``work/<member_slug>/``,
not the shared member workspace (which mixes internal data with scratch).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.common.projectless_workspace import (
    allocate_task_workspace,
    slugify,
)


_ARTIFACTS_SUBDIR = "artifacts"
_WORK_SUBDIR = "work"
_OUTPUTS_SUBDIR = "outputs"
_REGISTRY_SUBDIR = ".team_artifacts"


@dataclass(frozen=True, slots=True)
class TeamArtifactWorkspace:
    """Shared root and deliverables directory exposed to one team session.

    Both fields are per-team (shared by every member). A member's own temporary
    working directory is per-member and resolved separately via
    :func:`resolve_member_work_dir`, because the allocator runs before the
    per-member view exists.
    """

    root_dir: Path
    outputs_dir: Path


def get_team_artifacts_dir(team_ws_root: str | os.PathLike[str]) -> Path:
    """Return the artifacts root of a team workspace.

    The artifacts root is always ``<team-workspace>/artifacts``. Allocation of
    dated ``chat-<n>`` sub-directories underneath happens in
    :func:`get_team_artifact_workspace`.
    """
    return (Path(team_ws_root) / _ARTIFACTS_SUBDIR).resolve()


def get_team_artifact_workspace(
    team_ws_root: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    task_name: str | None = None,
) -> TeamArtifactWorkspace:
    """Create or reuse the shared deliverables directory for a team session.

    Reuses the same ``chat-<n>`` directory for the duration of a team session
    (all members share one), keyed by the team session id. Delegates the
    dated-directory / marker / metadata / registry allocation to the
    single-agent projectless allocator, only retargeting its root to the team
    workspace's ``artifacts/`` and co-locating the registry there so a
    relocated team workspace stays self-contained.

    Args:
        team_ws_root: The team workspace root path.
        session_id: The team session id (shared by every member of the team).
        task_name: Optional human-readable task/query title persisted to
            ``metadata.json`` (never used in the path).

    Returns:
        A :class:`TeamArtifactWorkspace` with the shared root and the created
        ``outputs/`` directory. A member's own ``work/<member_slug>/`` is not
        created here — call :func:`resolve_member_work_dir` per member.
    """
    artifacts_dir = get_team_artifacts_dir(team_ws_root)
    registry_dir = artifacts_dir / _REGISTRY_SUBDIR
    root = allocate_task_workspace(
        artifacts_dir,
        registry_dir=registry_dir,
        session_id=session_id,
        task_name=task_name,
    )
    outputs_dir = root / _OUTPUTS_SUBDIR
    outputs_dir.mkdir(parents=True, exist_ok=True)
    return TeamArtifactWorkspace(root_dir=root, outputs_dir=outputs_dir)


def resolve_member_work_dir(
    root_dir: str | os.PathLike[str],
    member_name: str | None,
) -> Path:
    """Create or reuse a member's isolated temporary working directory.

    Each member of a projectless team runs in its own ``work/<member_slug>/``
    under the shared artifact root, so intermediate files, caches and scratch
    scripts stay isolated instead of piling up together. The directory is
    created on first call; later calls reuse it.

    Args:
        root_dir: The shared artifact root (``TeamArtifactWorkspace.root_dir``).
        member_name: The member's semantic name. Falls back to ``"default"``
            when empty, so a nameless call still gets a real directory.

    Returns:
        The created/reused per-member working directory.
    """
    safe_member = slugify(
        member_name,
        fallback="default",
        reserved_prefix="member",
    )
    work_dir = Path(root_dir) / _WORK_SUBDIR / safe_member
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


__all__ = [
    "TeamArtifactWorkspace",
    "get_team_artifacts_dir",
    "get_team_artifact_workspace",
    "resolve_member_work_dir",
]
