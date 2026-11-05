# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped tenant context keyed by ``tenant_key``.

Design surface: ``TenantContext.bind(tenant_key)`` / ``reset()``.
Internal storage remains ContextVar-based (compatible with async tasks).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_TENANT_KEY_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_key", default=None
)
_TENANT_AGENT_WORKSPACE_ROOT_CV: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("tenant_agent_workspace_root", default=None)
)
_TENANT_AGENT_ROOT_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_agent_root", default=None
)
_TENANT_ROOT_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_root", default=None
)


@dataclass(frozen=True)
class _TenantContextTokens:
    tenant_key: contextvars.Token
    agent_workspace_root: contextvars.Token
    agent_root: contextvars.Token
    tenant_root: contextvars.Token


class TenantContext:
    """Request-scoped tenant binding (tenant_key + workspace paths)."""

    __slots__ = ("_tokens",)

    def __init__(self, tokens: _TenantContextTokens) -> None:
        self._tokens = tokens

    @property
    def tenant_key(self) -> str | None:
        return _TENANT_KEY_CV.get()

    @classmethod
    def current_tenant_key(cls) -> str | None:
        return _TENANT_KEY_CV.get()

    @classmethod
    def current_agent_workspace_root(cls) -> Path | None:
        bound = _TENANT_AGENT_WORKSPACE_ROOT_CV.get()
        return Path(bound) if bound else None

    @classmethod
    def current_agent_root(cls) -> Path | None:
        bound = _TENANT_AGENT_ROOT_CV.get()
        return Path(bound) if bound else None

    @classmethod
    def current_tenant_root(cls) -> Path | None:
        bound = _TENANT_ROOT_CV.get()
        return Path(bound) if bound else None

    @classmethod
    def bind(cls, tenant_key: str) -> "TenantContext":
        """Bind ``tenant_key`` and (enterprise) derived workspace paths."""
        from jiuwenswarm.common.utils import (
            get_agent_root_dir,
            get_agent_workspace_dir,
        )
        from jiuwenswarm.enterprise import is_enterprise
        from jiuwenswarm.enterprise.tenant.tenant_paths import (
            build_tenant_key,
            get_multi_tenant_user_workspace_dir,
        )

        key = str(tenant_key or "").strip() or build_tenant_key()

        if is_enterprise():
            tenant_root = get_multi_tenant_user_workspace_dir(key)
            agent_root = get_agent_root_dir(key)
            workspace = get_agent_workspace_dir(key)
            tenant_root.mkdir(parents=True, exist_ok=True)
            agent_root.mkdir(parents=True, exist_ok=True)
            workspace.mkdir(parents=True, exist_ok=True)
            tokens = _TenantContextTokens(
                tenant_key=_TENANT_KEY_CV.set(key),
                agent_workspace_root=_TENANT_AGENT_WORKSPACE_ROOT_CV.set(
                    str(workspace)
                ),
                agent_root=_TENANT_AGENT_ROOT_CV.set(str(agent_root)),
                tenant_root=_TENANT_ROOT_CV.set(str(tenant_root)),
            )
        else:
            # Personal: bind key only; path CVs stay unbound so getters keep develop layout.
            tokens = _TenantContextTokens(
                tenant_key=_TENANT_KEY_CV.set(key),
                agent_workspace_root=_TENANT_AGENT_WORKSPACE_ROOT_CV.set(None),
                agent_root=_TENANT_AGENT_ROOT_CV.set(None),
                tenant_root=_TENANT_ROOT_CV.set(None),
            )
        return cls(tokens)

    def reset(self) -> None:
        """Restore previous ContextVar bindings."""
        _TENANT_KEY_CV.reset(self._tokens.tenant_key)
        _TENANT_AGENT_WORKSPACE_ROOT_CV.reset(self._tokens.agent_workspace_root)
        _TENANT_AGENT_ROOT_CV.reset(self._tokens.agent_root)
        _TENANT_ROOT_CV.reset(self._tokens.tenant_root)

    @classmethod
    @contextmanager
    def scoped(cls, tenant_key: str) -> Iterator["TenantContext"]:
        ctx = cls.bind(tenant_key)
        try:
            yield ctx
        finally:
            ctx.reset()

    @classmethod
    def clear(cls) -> None:
        """Reset all tenant ContextVars (tests / teardown safety)."""
        _TENANT_KEY_CV.set(None)
        _TENANT_AGENT_WORKSPACE_ROOT_CV.set(None)
        _TENANT_AGENT_ROOT_CV.set(None)
        _TENANT_ROOT_CV.set(None)
