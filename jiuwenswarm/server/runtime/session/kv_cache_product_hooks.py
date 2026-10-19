# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Product-session hooks for Ascend KV cache affinity."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.runtime.session import session_history, session_metadata
from jiuwenswarm.server.utils.utils import is_team_params

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionSwitchContext:
    """Facts needed by the product owner and its optional KVC hooks."""

    target_is_team: bool
    previous_is_team: bool
    resolved_mode: str
    affinity_enabled: bool


async def cancel_pending_tasks() -> None:
    """Best-effort cleanup for all Agent-side KVC signal registries."""
    cleanup_callbacks = []
    try:
        from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
            cancel_pending_kv_cache_lifecycle_tasks,
        )

        cleanup_callbacks.append(cancel_pending_kv_cache_lifecycle_tasks)
    except Exception as exc:
        logger.warning("[ProductKVCacheHooks] root cleanup unavailable: %s", exc)
    try:
        from openjiuwen.core.foundation.kv_cache import (
            cancel_pending_session_kv_cache_signals,
        )

        cleanup_callbacks.append(cancel_pending_session_kv_cache_signals)
    except Exception as exc:
        logger.warning("[ProductKVCacheHooks] Plan cleanup unavailable: %s", exc)
    try:
        from openjiuwen.agent_teams.kv_cache.kv_cache_lifecycle import (
            cancel_pending_signal_tasks,
        )

        cleanup_callbacks.append(cancel_pending_signal_tasks)
    except Exception as exc:
        logger.warning("[ProductKVCacheHooks] Team cleanup unavailable: %s", exc)

    for cleanup in cleanup_callbacks:
        try:
            await cleanup()
        except Exception as exc:
            logger.warning(
                "[ProductKVCacheHooks] pending task cleanup failed: cleanup=%s error=%s",
                getattr(cleanup, "__name__", type(cleanup).__name__),
                exc,
            )


async def evict_plan_session(
    *,
    session_id: str,
    agent: Any = None,
    agent_manager: Any = None,
    channel_id: str | None = None,
) -> bool:
    """Best-effort evict for a permanently deleted non-Team session."""
    try:
        from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
            evict_session_kv_cache,
            is_kv_cache_affinity_enabled,
        )

        if not is_kv_cache_affinity_enabled():
            return False
        if agent is None and agent_manager is not None:
            try:
                agent = agent_manager.get_agent_nowait(channel_id)
            except Exception as exc:
                logger.warning(
                    "[ProductKVCacheHooks] live Plan agent unavailable for delete; "
                    "falling back to configured model: channel_id=%s error=%s",
                    channel_id,
                    exc,
                )
        result = await evict_session_kv_cache(
            session_id=session_id,
            parent_session_id=session_id,
            agent=agent,
        )
        return result.ok
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] Plan session evict failed; preserving delete: "
            "session_id=%s error=%s",
            session_id,
            exc,
        )
        return False


def resolve_session_switch_context(
    *,
    target_session_id: str,
    previous_session_id: str,
    params: dict[str, Any],
) -> SessionSwitchContext:
    """Resolve switch facts without changing the product runtime."""
    from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
        is_kv_cache_affinity_enabled,
    )

    try:
        affinity_enabled = is_kv_cache_affinity_enabled()
    except Exception as exc:
        affinity_enabled = False
        logger.warning(
            "[ProductKVCacheHooks] affinity gate failed; KVC actions skipped: "
            "session_id=%s error=%s",
            target_session_id,
            exc,
        )

    target_mode_params = {"mode": params.get("mode"), "team": params.get("team")}
    target_is_team = is_team_params(target_mode_params)
    requested_mode = str(params.get("mode") or "agent.plan")
    if not affinity_enabled:
        return SessionSwitchContext(
            target_is_team=target_is_team,
            previous_is_team=False,
            resolved_mode="team" if target_is_team else requested_mode,
            affinity_enabled=False,
        )

    target_metadata: dict[str, Any] = {}
    previous_metadata: dict[str, Any] = {}
    try:
        target_metadata = session_metadata.get_session_metadata(target_session_id)
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] target metadata unavailable; "
            "using request mode: session_id=%s error=%s",
            target_session_id,
            exc,
        )

    if (
        previous_session_id
        and previous_session_id not in {"new", target_session_id}
    ):
        try:
            previous_metadata = session_metadata.get_session_metadata(previous_session_id)
        except Exception as exc:
            logger.warning(
                "[ProductKVCacheHooks] previous metadata unavailable; "
                "using request mode: session_id=%s error=%s",
                previous_session_id,
                exc,
            )

    previous_mode_params = {"mode": params.get("previous_mode")}
    target_is_team = is_team_params(target_mode_params) or is_team_params(target_metadata)
    previous_is_team = (
        is_team_params(previous_mode_params) or is_team_params(previous_metadata)
    )

    resolved_mode = "team" if target_is_team else str(
        target_metadata.get("mode") or params.get("mode") or "agent.plan"
    )
    return SessionSwitchContext(
        target_is_team=target_is_team,
        previous_is_team=previous_is_team,
        resolved_mode=resolved_mode,
        affinity_enabled=affinity_enabled,
    )


async def dispatch_session_switch_signals(
    *,
    context: SessionSwitchContext,
    agent_manager: Any,
    channel_id: str,
    team_manager: Any,
    target_session_id: str,
    previous_session_id: str,
    reason: str,
) -> None:
    """Send optional KVC signals after the product owner handles the switch."""
    if not context.affinity_enabled:
        return

    from jiuwenswarm.server.runtime.session.kv_cache_affinity_lifecycle import (
        dispatch_offload_session_kv_cache,
        dispatch_prefetch_session_kv_cache,
    )

    try:
        agent = None
        needs_plan_model = (
            previous_session_id
            and previous_session_id not in {"new", target_session_id}
            and not context.previous_is_team
        ) or not context.target_is_team
        if needs_plan_model:
            try:
                agent = agent_manager.get_agent_nowait(channel_id)
            except Exception as exc:
                logger.warning(
                    "[ProductKVCacheHooks] live Plan agent unavailable; "
                    "falling back to configured model: channel_id=%s error=%s",
                    channel_id,
                    exc,
                )
        if (
            previous_session_id
            and previous_session_id not in {"new", target_session_id}
            and not context.previous_is_team
        ):
            dispatch_offload_session_kv_cache(
                session_id=previous_session_id,
                parent_session_id=previous_session_id,
                agent=agent,
            )

        if session_history.history_exists(target_session_id):
            if context.target_is_team and team_manager is not None:
                await team_manager.prefetch_session_kv_cache(
                    target_session_id,
                    reason=reason,
                )
            elif not context.target_is_team:
                dispatch_prefetch_session_kv_cache(
                    session_id=target_session_id,
                    parent_session_id=target_session_id,
                    agent=agent,
                )
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] session switch signal failed; continuing: "
            "target_session_id=%s previous_session_id=%s error=%s",
            target_session_id,
            previous_session_id,
            exc,
        )
