# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped tenant context keyed by ``tenant_key``.

Design surface: ``TenantContext.bind(tenant_key, *, group_id, bot_id, user_id)``
/ ``reset()``. Internal storage remains ContextVar-based (compatible with
async tasks).

The plaintext routing triple ``(group_id, bot_id, user_id)`` lives on the same
envelope so downstream features (workspace quota, etc.) can reuse identity
without a parallel ContextVar stack.
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
_TENANT_GROUP_ID_CV: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tenant_group_id", default=""
)
_TENANT_BOT_ID_CV: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tenant_bot_id", default=""
)
_TENANT_USER_ID_CV: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tenant_user_id", default=""
)


@dataclass(frozen=True)
class _TenantContextTokens:
    tenant_key: contextvars.Token
    agent_workspace_root: contextvars.Token
    agent_root: contextvars.Token
    tenant_root: contextvars.Token
    group_id: contextvars.Token
    bot_id: contextvars.Token
    user_id: contextvars.Token


class TenantContext:
    """Request-scoped tenant binding (tenant_key + paths + plaintext triple)."""

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
    def current_group_id(cls) -> str:
        return _TENANT_GROUP_ID_CV.get() or ""

    @classmethod
    def current_bot_id(cls) -> str:
        return _TENANT_BOT_ID_CV.get() or ""

    @classmethod
    def current_user_id(cls) -> str:
        return _TENANT_USER_ID_CV.get() or ""

    @classmethod
    def current_triple(cls) -> tuple[str, str, str]:
        """Return bound plaintext ``(group_id, bot_id, user_id)`` (may be empty)."""
        return (cls.current_group_id(), cls.current_bot_id(), cls.current_user_id())

    @classmethod
    def bind(
        cls,
        tenant_key: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> "TenantContext":
        """Bind ``tenant_key``, optional plaintext triple, and (enterprise) paths."""
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
        gid = str(group_id or "").strip()
        bid = str(bot_id or "").strip()
        uid = str(user_id or "").strip()

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
                group_id=_TENANT_GROUP_ID_CV.set(gid),
                bot_id=_TENANT_BOT_ID_CV.set(bid),
                user_id=_TENANT_USER_ID_CV.set(uid),
            )
        else:
            # Personal: bind key + optional identity; path CVs stay unbound
            # so getters keep develop layout.
            tokens = _TenantContextTokens(
                tenant_key=_TENANT_KEY_CV.set(key),
                agent_workspace_root=_TENANT_AGENT_WORKSPACE_ROOT_CV.set(None),
                agent_root=_TENANT_AGENT_ROOT_CV.set(None),
                tenant_root=_TENANT_ROOT_CV.set(None),
                group_id=_TENANT_GROUP_ID_CV.set(gid),
                bot_id=_TENANT_BOT_ID_CV.set(bid),
                user_id=_TENANT_USER_ID_CV.set(uid),
            )
        return cls(tokens)

    def reset(self) -> None:
        """Restore previous ContextVar bindings."""
        _TENANT_KEY_CV.reset(self._tokens.tenant_key)
        _TENANT_AGENT_WORKSPACE_ROOT_CV.reset(self._tokens.agent_workspace_root)
        _TENANT_AGENT_ROOT_CV.reset(self._tokens.agent_root)
        _TENANT_ROOT_CV.reset(self._tokens.tenant_root)
        _TENANT_GROUP_ID_CV.reset(self._tokens.group_id)
        _TENANT_BOT_ID_CV.reset(self._tokens.bot_id)
        _TENANT_USER_ID_CV.reset(self._tokens.user_id)

    @classmethod
    @contextmanager
    def scoped(
        cls,
        tenant_key: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> Iterator["TenantContext"]:
        ctx = cls.bind(
            tenant_key, group_id=group_id, bot_id=bot_id, user_id=user_id
        )
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
        _TENANT_GROUP_ID_CV.set("")
        _TENANT_BOT_ID_CV.set("")
        _TENANT_USER_ID_CV.set("")
