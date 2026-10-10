"""Shared runtime contract for command working directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.common.utils import get_agent_workspace_dir


@dataclass(frozen=True)
class CommandRuntimePaths:
    """Host-owned path anchors used by command execution."""

    current_cwd: Path
    project_root: Path
    workspace_root: Path | None
    agent_workspace_root: Path


def current_command_runtime_paths(
    *, require_runtime_cwd: bool = False
) -> CommandRuntimePaths:
    """Read the current OpenJiuwen command path context once."""

    current_cwd = _current_cwd(require_runtime_cwd=require_runtime_cwd)
    return CommandRuntimePaths(
        current_cwd=current_cwd,
        project_root=_current_project_root(current_cwd),
        workspace_root=_current_workspace_root(),
        agent_workspace_root=get_agent_workspace_dir().resolve(),
    )


def resolve_command_workdir(
    workdir: object,
    *,
    runtime_paths: CommandRuntimePaths,
) -> Path:
    """Resolve one command workdir using the tool's existing root contract."""

    if not isinstance(runtime_paths, CommandRuntimePaths):
        raise ValueError("command runtime paths are unavailable")
    if workdir is None or workdir == "":
        candidate = runtime_paths.current_cwd
    elif isinstance(workdir, str):
        candidate = Path(workdir)
    else:
        raise ValueError("workdir must be a string")
    if not candidate.is_absolute():
        candidate = runtime_paths.current_cwd / candidate
    candidate = candidate.resolve()

    allowed_roots = [runtime_paths.project_root]
    if runtime_paths.workspace_root is not None:
        allowed_roots.append(runtime_paths.workspace_root)
    allowed_roots.append(runtime_paths.agent_workspace_root)
    if not any(_is_relative_to(candidate, root) for root in allowed_roots):
        raise ValueError("workdir is outside project workspace")
    return candidate


def _current_cwd(*, require_runtime_cwd: bool) -> Path:
    try:
        from openjiuwen.core.sys_operation.cwd import get_cwd

        return Path(get_cwd()).resolve()
    except Exception:
        if require_runtime_cwd:
            raise ValueError("command runtime cwd is unavailable") from None
        return get_agent_workspace_dir().resolve()


def _current_project_root(current_cwd: Path) -> Path:
    try:
        from openjiuwen.core.sys_operation.cwd import get_project_root

        return Path(get_project_root()).resolve()
    except Exception:
        return current_cwd


def _current_workspace_root() -> Path | None:
    try:
        from openjiuwen.core.sys_operation.cwd import get_workspace

        workspace = get_workspace()
        return Path(workspace).resolve() if workspace else None
    except Exception:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
