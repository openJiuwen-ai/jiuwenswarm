# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Best-effort KVC hooks for Team product lifecycle owners."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from openjiuwen.core.runner import Runner

logger = logging.getLogger(__name__)

TeamKVCSignal = Literal["offload", "prefetch"]
TeamNameResolver = Callable[[str], str | None]
StopRuntime = Callable[..., Awaitable[bool]]


def is_enabled() -> bool:
    """Fail closed without affecting the owning Team lifecycle."""
    try:
        from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
            is_kv_cache_affinity_enabled,
        )

        return is_kv_cache_affinity_enabled()
    except Exception as exc:
        logger.warning(
            "[TeamKVCacheHooks] affinity gate failed; preserving lifecycle: error=%s",
            exc,
        )
        return False


async def stop_runtime_before_terminal_delete(
    stop_runtime: StopRuntime,
    *,
    session_id: str,
    reason: str,
) -> bool:
    """Preserve bindings only when terminal KVC eviction needs them.

    Disabled affinity uses the original stop call unchanged. Enabled affinity
    leaves the Runner entry alive for agent-core's delete hook to snapshot and
    then stop.
    """
    if is_enabled():
        return await stop_runtime(
            session_id,
            reason=reason,
            stop_runner=False,
        )
    return await stop_runtime(session_id, reason=reason)


async def dispatch_for_session(
    action: TeamKVCSignal,
    *,
    session_id: str,
    reason: str,
    resolve_team_name: TeamNameResolver,
) -> bool:
    """Resolve a Team binding only after the affinity gate succeeds."""
    if not is_enabled():
        return False
    try:
        team_name = resolve_team_name(session_id)
    except Exception as exc:
        logger.warning(
            "[TeamKVCacheHooks] binding lookup failed; preserving lifecycle: "
            "action=%s session_id=%s error=%s",
            action,
            session_id,
            exc,
        )
        return False
    return await dispatch_signal(
        action,
        session_id=session_id,
        team_name=team_name,
        reason=reason,
        gate_checked=True,
    )


async def dispatch_signal(
    action: TeamKVCSignal,
    *,
    session_id: str,
    team_name: str | None,
    reason: str,
    gate_checked: bool = False,
) -> bool:
    """Dispatch a Team signal using only an already known binding."""
    if (not gate_checked and not is_enabled()) or not team_name:
        return False
    try:
        return await Runner.dispatch_agent_team_kv_cache(
            action=action,
            team_name=team_name,
            session_id=session_id,
            reason=reason,
        )
    except Exception as exc:
        logger.warning(
            "[TeamKVCacheHooks] signal dispatch failed; preserving lifecycle: "
            "action=%s session_id=%s team_name=%s error=%s",
            action,
            session_id,
            team_name,
            exc,
        )
        return False
