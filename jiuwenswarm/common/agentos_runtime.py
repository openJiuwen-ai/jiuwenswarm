"""Trusted deployment context for AgentOS-only Runtime behavior."""

from __future__ import annotations

import os

RUNTIME_PROFILE_ENV = "JIUWENSWARM_RUNTIME_PROFILE"
AGENTOS_DEFAULT_WORKSPACE = "/home/agentos/workspace"


def is_agentos_runtime() -> bool:
    return os.getenv(RUNTIME_PROFILE_ENV, "").strip().lower() == "agentos"


def is_agentos_workspace_fallback(
    *, channel_id: str | None, project_dir: str | None, project_id: str | None,
) -> bool:
    """A cloud Web cwd hint is not a persistent project binding.

    Only the known default workspace is special. Legacy directory-only sessions
    and all other channels keep their existing binding rules.
    """
    return (
        is_agentos_runtime()
        and channel_id == "web"
        and (project_id or "").strip() in {"", "default", "default_code"}
        and (project_dir or "").strip().rstrip("/") == AGENTOS_DEFAULT_WORKSPACE
    )
