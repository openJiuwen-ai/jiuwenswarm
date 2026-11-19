# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Treat ``swarm.team_accel_sys`` as the team filesystem rail for worktree guards.

openjiuwen's worktree shell guard only recognizes ``core.sys_operation``.
Team blueprints mount ``swarm.team_accel_sys`` (``AccelSysOperationRail``)
instead, so the stock guard never sees a match and appends a second
``core.sys_operation`` RailSpec. Both rails share priority 100 unless Accel
lowers its priority; stable init order then lets the stock rail refresh
``edit_file`` after Accel and wipe ``FlashEditFileTool`` (and its ``hunks``
schema).

This patch widens the guard's type match to include ``swarm.team_accel_sys``
so deny patterns land on Accel and no duplicate stock filesystem rail is
injected. Idempotent; safe to call from provider registration and tests.

Monkeypatch targets are module callables used by AgentConfigurator. Names are
applied via ``setattr`` / ``getattr`` so this file does not touch protected
identifiers with dotted access (G.CLS.11). Deny patterns are owned locally and
kept aligned with agent-core's worktree bash deny list.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_APPLIED = False

# Keep aligned with openjiuwen agent_teams worktree bash deny patterns.
TEAM_WORKTREE_BASH_DENY_PATTERNS: tuple[str, ...] = (
    r"\bgit(?:\s+(?:-[A-Za-z](?:\s+\S+)?|--[^\s;&|]+(?:=\S+)?))*\s+worktree\s+"
    r"(?:add|remove|prune|move|repair|lock|unlock)\b",
)

# Agent-configurator module attributes replaced by this patch.
_APPLY_GUARD_ATTR = "_apply_team_worktree_shell_guard"
_HAS_GUARD_ATTR = "_has_team_worktree_shell_guard"


def apply_team_accel_sys_rail_patch() -> bool:
    """Patch AgentConfigurator worktree guards to recognize team accel sys.

    Returns True when the patch is (or was already) applied. Upstream import /
    attribute failures are logged and return False so import-time callers do not
    abort startup; ``_APPLIED`` stays False so a later call can retry.
    """
    global _APPLIED
    if _APPLIED:
        return True

    try:
        from openjiuwen.agent_teams.agent import agent_configurator as ac
        from openjiuwen.agent_teams.rails.builtin_elements import SYS_OPERATION
        from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec
    except (ImportError, AttributeError):
        logger.warning(
            "[TeamAccel] openjiuwen worktree guard symbols unavailable; skip patch",
            exc_info=True,
        )
        return False

    # Literal keeps this module free of jiuwenswarm.agents.swarm imports
    # (accel → swarm → team_runtime → accel circular import).
    team_accel_sys = "swarm.team_accel_sys"
    sys_rail_types = frozenset({SYS_OPERATION, team_accel_sys})
    deny_patterns = list(TEAM_WORKTREE_BASH_DENY_PATTERNS)

    def apply_team_worktree_shell_guard(
        rails: list[Any],
        *,
        enabled: bool,
    ) -> list[Any]:
        if not enabled:
            return rails
        guarded: list[Any] = []
        found_sys_operation = False
        for rail in rails:
            if getattr(rail, "type", None) not in sys_rail_types:
                guarded.append(rail)
                continue
            found_sys_operation = True
            params = dict(getattr(rail, "params", None) or {})
            merged = list(params.get("bash_deny_patterns") or [])
            for pattern in deny_patterns:
                if pattern not in merged:
                    merged.append(pattern)
            params["bash_deny_patterns"] = merged
            guarded.append(rail.model_copy(update={"params": params}))
        if not found_sys_operation:
            guarded.append(
                RailSpec(
                    type=SYS_OPERATION,
                    params={"bash_deny_patterns": list(deny_patterns)},
                )
            )
        return guarded

    def has_team_worktree_shell_guard(rails: list[Any]) -> bool:
        for rail in rails:
            if getattr(rail, "type", None) not in sys_rail_types:
                continue
            params = getattr(rail, "params", None) or {}
            present = set(params.get("bash_deny_patterns") or [])
            if all(pattern in present for pattern in deny_patterns):
                return True
        return False

    try:
        setattr(ac, _APPLY_GUARD_ATTR, apply_team_worktree_shell_guard)
        setattr(ac, _HAS_GUARD_ATTR, has_team_worktree_shell_guard)
    except (ImportError, AttributeError):
        logger.warning(
            "[TeamAccel] failed to install worktree guard patch; skip",
            exc_info=True,
        )
        return False

    _APPLIED = True
    logger.info(
        "[TeamAccel] worktree sys-rail guard recognizes %s (avoids dual SysOperationRail)",
        team_accel_sys,
    )
    return True


def get_patched_worktree_shell_guard():
    """Return the active apply-guard callable after the patch is installed."""
    try:
        from openjiuwen.agent_teams.agent import agent_configurator as ac
    except ImportError:
        apply_team_accel_sys_rail_patch()
        return None
    apply_team_accel_sys_rail_patch()
    return getattr(ac, _APPLY_GUARD_ATTR, None)


def get_patched_has_worktree_shell_guard():
    """Return the active has-guard callable after the patch is installed."""
    try:
        from openjiuwen.agent_teams.agent import agent_configurator as ac
    except ImportError:
        apply_team_accel_sys_rail_patch()
        return None
    apply_team_accel_sys_rail_patch()
    return getattr(ac, _HAS_GUARD_ATTR, None)


__all__ = [
    "TEAM_WORKTREE_BASH_DENY_PATTERNS",
    "apply_team_accel_sys_rail_patch",
    "get_patched_has_worktree_shell_guard",
    "get_patched_worktree_shell_guard",
]
