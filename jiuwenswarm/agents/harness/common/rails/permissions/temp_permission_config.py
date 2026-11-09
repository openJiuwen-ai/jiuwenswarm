# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assemble one-shot permission ``config=`` for Manager ``check_permission``.

This is the **only** place business code should merge session / Skill overlays
into a temporary config for a single check.  The result is passed as
``config=`` and is never registered as a Manager key.

Returns ``None`` when there is no overlay to apply — callers then check with
the user-level key alone.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Mapping, Protocol

__all__ = [
    "assemble_temp_permission_config",
]

logger = logging.getLogger(__name__)


class _PublishedScopesLike(Protocol):
    def has_active_grant(self, session_id: str, scope: str) -> bool:
        ...

    def get_active(self, session_id: str, scope: str) -> Any:
        ...


def assemble_temp_permission_config(
    base_config: Mapping[str, Any],
    *,
    session_id: str | None = None,
    agent_scope_id: str | None = None,
    published_scopes: _PublishedScopesLike | None = None,
) -> dict[str, Any] | None:
    """Merge session overlay and active Skill overlay for one permission check.

    Args:
        base_config: User/agent-level permissions body already on the Manager key.
        session_id: Current session (enterprise overlay lookup).
        agent_scope_id: Skill scope id (``main`` / subagent scope); empty skips Skill.
        published_scopes: Grant/active-skill view used to read Skill overlay snapshot.

    Returns:
        A deep-copied merged dict when any overlay applies; otherwise ``None``.
    """
    from openjiuwen.harness.security.skill_authorization import (
        compose_skill_permissions,
        is_skill_authorization_enabled,
    )

    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        has_session_permissions_overlay,
        merge_session_permissions_overlay,
    )

    if not isinstance(base_config, Mapping):
        return None

    base = copy.deepcopy(dict(base_config))
    used_temp = False

    if session_id and has_session_permissions_overlay(session_id):
        base = merge_session_permissions_overlay(base, session_id=session_id)
        used_temp = True

    if not session_id or not agent_scope_id:
        return base if used_temp else None
    if published_scopes is None:
        return base if used_temp else None
    if not is_skill_authorization_enabled(base):
        return base if used_temp else None
    if not published_scopes.has_active_grant(session_id, agent_scope_id):
        return base if used_temp else None

    active = published_scopes.get_active(session_id, agent_scope_id)
    overlay = getattr(active, "overlay_snapshot", None) if active is not None else None
    if overlay:
        base = compose_skill_permissions(base, overlay)
        used_temp = True
    return base if used_temp else None
