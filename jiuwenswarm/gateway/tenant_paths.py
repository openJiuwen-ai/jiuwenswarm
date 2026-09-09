# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lazy per-message tenant path helpers for Gateway channels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import (
    get_multi_tenant_user_workspace_dir,
    normalize_tenant_scope_id,
)


def normalize_channel_tenant_ids(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[str, str]:
    """Always return a concrete ``(service_id, agent_id)`` pair (default/default)."""
    return normalize_tenant_scope_id(service_id), normalize_tenant_scope_id(agent_id)


def workspace_key_from_channel_ids(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """Map Gateway routing ``(service_id, agent_id)`` to a disk ``workspace_key``.

    Preserves prior 2D isolation under the single ``workspace_{key}/`` layout:
    ``default``+``default`` → ``default``; otherwise ``{sid}_{aid}``.
    """
    sid, aid = normalize_channel_tenant_ids(service_id, agent_id)
    if sid == "default" and aid == "default":
        return "default"
    return f"{sid}_{aid}"


def tenant_ids_from_im_identity(
    chat_id: str,
    bot_id: str,
    user_id: str = "",
) -> tuple[str, str]:
    """Derive tenant ids the same way SessionMap does for IM traffic."""
    from jiuwenswarm.gateway.routing.session_map import (
        invoke_ids_from_identity,
        load_session_map_scope,
    )

    sid, aid = invoke_ids_from_identity(
        str(chat_id or "").strip(),
        str(bot_id or "").strip(),
        str(user_id or "").strip(),
        load_session_map_scope(),
    )
    return normalize_channel_tenant_ids(sid, aid)


def tenant_ids_from_message(msg: Any) -> tuple[str, str]:
    """Prefer ``msg.params`` sid/aid; else parse SessionMap ``session_id``; else default."""
    params = getattr(msg, "params", None)
    if isinstance(params, dict) and (
        params.get("service_id") is not None or params.get("agent_id") is not None
    ):
        return normalize_channel_tenant_ids(
            params.get("service_id"),
            params.get("agent_id"),
        )
    session_id = str(getattr(msg, "session_id", None) or "").strip()
    if session_id and "::" in session_id:
        from jiuwenswarm.gateway.routing.session_map import invoke_ids_from_session_id_string

        sid, aid = invoke_ids_from_session_id_string(session_id)
        return normalize_channel_tenant_ids(sid, aid)
    return "default", "default"


def resolve_channel_agent_workspace(workspace_key: str | None = None) -> Path:
    """``<tenant_root>/agent/jiuwenclaw_workspace``（经 ``get_multi_tenant_user_workspace_dir``）。"""
    wk = normalize_tenant_scope_id(workspace_key)
    base = get_multi_tenant_user_workspace_dir(wk)
    return base / "agent" / "jiuwenclaw_workspace"


def resolve_channel_group_chat_memory_dir(workspace_key: str | None = None) -> Path:
    """Per-tenant group chat memory root under agent workspace."""
    return resolve_channel_agent_workspace(workspace_key) / "memory" / "group_chat"


__all__ = [
    "normalize_channel_tenant_ids",
    "workspace_key_from_channel_ids",
    "tenant_ids_from_im_identity",
    "tenant_ids_from_message",
    "resolve_channel_agent_workspace",
    "resolve_channel_group_chat_memory_dir",
]
