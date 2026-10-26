# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped tenant workspace bindings via ContextVar."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path

_TENANT_JIUWENCLAW_WS_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_jiuwenclaw_workspace", default=None
)
_TENANT_AGENT_ROOT_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_agent_root", default=None
)
_TENANT_ROOT_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_root", default=None
)
_WORKSPACE_KEY_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_workspace_key", default=None
)


@dataclass(frozen=True)
class TenantContextTokens:
    jiuwenclaw_ws: contextvars.Token
    agent_root: contextvars.Token
    tenant_root: contextvars.Token


def bind_tenant_workspace_dirs(
    *,
    jiuwenclaw_workspace: str,
    agent_root: str,
    tenant_root: str,
) -> TenantContextTokens:
    """Bind tenant workspace paths for the current async task."""
    return TenantContextTokens(
        jiuwenclaw_ws=_TENANT_JIUWENCLAW_WS_CV.set(jiuwenclaw_workspace),
        agent_root=_TENANT_AGENT_ROOT_CV.set(agent_root),
        tenant_root=_TENANT_ROOT_CV.set(tenant_root),
    )


def reset_tenant_workspace_dirs(token: TenantContextTokens) -> None:
    """Reset tenant workspace bindings."""
    _TENANT_JIUWENCLAW_WS_CV.reset(token.jiuwenclaw_ws)
    _TENANT_AGENT_ROOT_CV.reset(token.agent_root)
    _TENANT_ROOT_CV.reset(token.tenant_root)


def bind_workspace_key(workspace_key: str | None = None) -> contextvars.Token:
    """Bind disk ``workspace_key`` for the current task (``workspace_{key}/``)."""
    text = "default" if workspace_key is None else str(workspace_key).strip() or "default"
    if "__" in text:
        raise ValueError(f"workspace_key must not contain '__': {text!r}")
    return _WORKSPACE_KEY_CV.set(text)


def reset_workspace_key(token: contextvars.Token) -> None:
    """Restore the previous ``workspace_key`` binding."""
    _WORKSPACE_KEY_CV.reset(token)


def get_bound_workspace_key() -> str | None:
    """Return the currently bound ``workspace_key``, or None if unbound."""
    return _WORKSPACE_KEY_CV.get()


def get_bound_jiuwenclaw_workspace() -> Path | None:
    bound = _TENANT_JIUWENCLAW_WS_CV.get()
    return Path(bound) if bound else None


def get_bound_agent_root() -> Path | None:
    bound = _TENANT_AGENT_ROOT_CV.get()
    return Path(bound) if bound else None


def get_bound_tenant_root() -> Path | None:
    bound = _TENANT_ROOT_CV.get()
    return Path(bound) if bound else None


def clear_tenant_bindings() -> None:
    """Reset all tenant ContextVars (tests / request teardown safety)."""
    _TENANT_JIUWENCLAW_WS_CV.set(None)
    _TENANT_AGENT_ROOT_CV.set(None)
    _TENANT_ROOT_CV.set(None)
    _WORKSPACE_KEY_CV.set(None)
