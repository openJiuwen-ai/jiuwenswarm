"""Optional process-wide path provider; request identity remains task-local."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class PathCategory(str, Enum):
    LOGS = "logs"
    CHECKPOINT = "checkpoint"
    WORKSPACE = "workspace"
    AGENT_ROOT = "agent_root"
    MEMORY = "memory"
    CODING_MEMORY = "coding_memory"
    TODO = "todo"
    MESSAGES = "messages"
    AGENTS = "agents"
    SKILLS = "skills"
    SESSIONS = "sessions"
    INTERACTIONS = "interactions"
    EVOLUTION_TRAJECTORIES = "evolution_trajectories"
    PROMPT_ATTACHMENT = "prompt_attachment"
    PROJECT_WORKSPACE = "project_workspace"
    PROJECT_SESSION_WORKSPACE = "project_session_workspace"
    WORKSPACE_MD = "workspace_md"
    DEBUG_TRACE = "debug_trace"
    SHARED_SKILLS_DIRS = "shared_skills_dirs"


@dataclass(frozen=True)
class PathContext:
    service_id: str | None = None
    agent_id: str | None = None
    workspace_key: str | None = None
    session_id: str | None = None
    bound_agent_root: str | None = None
    bound_workspace: str | None = None


_session_id: ContextVar[str | None] = ContextVar("path_session_id", default=None)


def bind_path_session_id(session_id: str | None) -> Token:
    return _session_id.set(session_id)


def reset_path_session_id(token: Token) -> None:
    _session_id.reset(token)


def current_path_context(*, session_id: str | None = None) -> PathContext:
    from jiuwenswarm.common.local_env_config import get_bound_agent_env_ns
    from jiuwenswarm.server.runtime.tenant_context import (
        get_bound_agent_root,
        get_bound_jiuwenclaw_workspace,
        get_bound_workspace_key,
    )

    ns = get_bound_agent_env_ns()
    root = get_bound_agent_root()
    workspace = get_bound_jiuwenclaw_workspace()
    return PathContext(
        service_id=ns[0] if ns is not None else None,
        agent_id=ns[1] if ns is not None else None,
        workspace_key=get_bound_workspace_key(),
        session_id=session_id if session_id is not None else _session_id.get(),
        bound_agent_root=str(root) if root is not None else None,
        bound_workspace=str(workspace) if workspace is not None else None,
    )


@runtime_checkable
class PathProvider(Protocol):
    name: str

    def resolve_path(
        self,
        category: PathCategory,
        ctx: PathContext,
        *,
        node: str | None = None,
        session_id: str | None = None,
    ) -> Path | None:
        return None

    def resolve_path_list(
        self,
        category: PathCategory,
        ctx: PathContext,
    ) -> list[Path] | None:
        return None

    def build_workspace_directories(self, ctx: PathContext) -> list[dict] | None:
        return None


_provider: PathProvider | None = None


def register_path_provider(provider: PathProvider) -> None:
    global _provider
    if _provider is not None:
        logger.warning("Replacing registered path provider")
    _provider = provider


def get_path_provider() -> PathProvider | None:
    return _provider


def reset_path_provider() -> None:
    global _provider
    _provider = None
