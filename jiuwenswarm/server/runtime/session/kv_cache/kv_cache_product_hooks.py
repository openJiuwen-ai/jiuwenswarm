# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Product-session adapters for optional KV cache affinity lifecycle hooks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from jiuwenswarm.server.runtime.session import session_history, session_metadata
from jiuwenswarm.server.utils.utils import is_team_params

logger = logging.getLogger(__name__)
_PRODUCT_GUARD_TASKS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class SessionSwitchContext:
    """Facts needed by the product owner and its optional KVC hooks."""

    target_is_team: bool
    previous_is_team: bool
    resolved_mode: str
    affinity_enabled: bool


async def cancel_pending_tasks() -> None:
    """Cancel product guard tasks; Runtime tasks are closed by the application."""
    tasks = tuple(_PRODUCT_GUARD_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        get_session_kv_cache_task_guard().clear()
    except Exception as exc:
        logger.warning("[ProductKVCacheHooks] task guard cleanup failed: %s", exc)


async def evict_plan_session(
    *,
    session_id: str,
) -> bool:
    """Best-effort evict for a permanently deleted non-Team session."""
    try:
        from openjiuwen.core.session.agent import create_agent_session
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
            get_kv_cache_runtime,
        )

        if not is_kv_cache_affinity_enabled():
            return False
        session = create_agent_session(
            session_id=session_id,
            kv_cache_runtime=get_kv_cache_runtime(),
        )
        return await session.release_kvc()
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
    from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
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
    previous_mode_params = {"mode": params.get("previous_mode")}
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

    if previous_session_id and previous_session_id not in {"new", target_session_id}:
        try:
            previous_metadata = session_metadata.get_session_metadata(
                previous_session_id
            )
        except Exception as exc:
            logger.warning(
                "[ProductKVCacheHooks] previous metadata unavailable; "
                "using request mode: session_id=%s error=%s",
                previous_session_id,
                exc,
            )

    target_is_team = is_team_params(target_mode_params) or is_team_params(
        target_metadata
    )
    previous_is_team = is_team_params(previous_mode_params) or is_team_params(
        previous_metadata
    )

    resolved_mode = (
        "team"
        if target_is_team
        else str(target_metadata.get("mode") or params.get("mode") or "agent.plan")
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
    channel_id: str,
    target_session_id: str,
    previous_session_id: str,
    view_id: str = "default-view",
) -> None:
    """Record a real foreground transition without directly switching KVC."""
    if not context.affinity_enabled:
        return
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        guard = get_session_kv_cache_task_guard()
        if previous_session_id and previous_session_id not in {
            "new",
            target_session_id,
        }:
            action = guard.set_foreground(
                session_id=previous_session_id,
                view_id=view_id,
                visible=False,
                channel_id=channel_id,
                is_team=context.previous_is_team,
                has_history=session_history.history_exists(previous_session_id),
            )
            _dispatch_guard_action(action)

        guard.set_foreground(
            session_id=target_session_id,
            view_id=view_id,
            visible=True,
            channel_id=channel_id,
            is_team=context.target_is_team,
            has_history=session_history.history_exists(target_session_id),
        )
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] session foreground update failed; continuing: "
            "target_session_id=%s previous_session_id=%s error=%s",
            target_session_id,
            previous_session_id,
            exc,
        )


async def record_chat_started(
    *,
    session_id: str,
    params: dict[str, Any],
    channel_id: str,
) -> None:
    """Record one top-level task start; never wait for prefetch/offload."""
    if not session_id:
        return
    from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
        is_kv_cache_affinity_enabled,
    )

    if not is_kv_cache_affinity_enabled():
        return
    is_team = _resolve_session_is_team(session_id, params)
    from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
        get_session_kv_cache_task_guard,
    )

    action = get_session_kv_cache_task_guard().task_started(
        session_id=session_id,
        channel_id=channel_id,
        is_team=is_team,
        has_history=session_history.history_exists(session_id),
    )
    _dispatch_guard_action(action)


def record_chat_finished(
    *,
    session_id: str,
    succeeded: bool,
) -> None:
    """Record authoritative top-level completion and offload if background."""
    if not session_id:
        return
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        if not is_kv_cache_affinity_enabled():
            return
        action = get_session_kv_cache_task_guard().task_finished(
            session_id=session_id,
            succeeded=succeeded,
        )
        _dispatch_guard_action(action)
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] chat completion update failed: session_id=%s error=%s",
            session_id,
            exc,
        )


def record_session_prepare(
    *,
    session_id: str,
    intent_id: str,
    channel_id: str,
    params: dict[str, Any],
) -> Literal["scheduled", "not_needed", "disabled", "failed"]:
    """Record typing intent and report whether it scheduled a prefetch."""
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        if not is_kv_cache_affinity_enabled():
            return "disabled"
        is_team = _resolve_session_is_team(session_id, params)
        guard = get_session_kv_cache_task_guard()
        guard.set_foreground(
            session_id=session_id,
            view_id=str(params.get("view_id") or "default-view"),
            visible=True,
            channel_id=channel_id,
            is_team=is_team,
            has_history=session_history.history_exists(session_id),
        )
        action = guard.prepare(
            session_id=session_id,
            intent_id=intent_id,
            channel_id=channel_id,
            is_team=is_team,
            has_history=session_history.history_exists(session_id),
        )
        _dispatch_guard_action(action)
        return "scheduled" if action is not None else "not_needed"
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] input intent update failed: session_id=%s error=%s",
            session_id,
            exc,
        )
        return "failed"


def mark_session_deleted(
    *,
    session_id: str,
    channel_id: str,
    is_team: bool,
) -> None:
    """Tombstone only in process memory; the existing delete owner runs evict."""
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )

        if not is_kv_cache_affinity_enabled():
            return
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        get_session_kv_cache_task_guard().delete(
            session_id=session_id,
            channel_id=channel_id,
            is_team=is_team,
        )
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] delete tombstone failed: session_id=%s error=%s",
            session_id,
            exc,
        )


def restore_session_after_failed_delete(session_id: str) -> None:
    """Restore KVC facts when the authoritative product delete did not commit."""
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )

        if not is_kv_cache_affinity_enabled():
            return
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        get_session_kv_cache_task_guard().restore_after_failed_delete(session_id)
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] failed-delete rollback failed: "
            "session_id=%s error=%s",
            session_id,
            exc,
        )


def _resolve_session_is_team(session_id: str, params: dict[str, Any]) -> bool:
    metadata: dict[str, Any] = {}
    try:
        metadata = session_metadata.get_session_metadata(session_id)
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] failed to resolve session metadata: "
            "session_id=%s error=%s",
            session_id,
            exc,
        )
    return is_team_params(
        {"mode": params.get("mode"), "team": params.get("team")}
    ) or is_team_params(metadata)


def forget_deleted_session(session_id: str) -> None:
    """Drop product KVC facts after the authoritative delete commits."""
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_task_guard import (
            get_session_kv_cache_task_guard,
        )

        get_session_kv_cache_task_guard().forget(session_id)
    except Exception as exc:
        logger.warning(
            "[ProductKVCacheHooks] deleted-session cleanup failed: "
            "session_id=%s error=%s",
            session_id,
            exc,
        )


def _dispatch_guard_action(action: Any) -> None:
    if action is None or action.action not in {"offload", "prefetch"}:
        return

    async def _dispatch() -> None:
        from openjiuwen.core.session.agent import create_agent_session
        from openjiuwen.core.session.agent_team import create_agent_team_session
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
            get_kv_cache_runtime,
        )

        factory = create_agent_team_session if action.is_team else create_agent_session
        session = factory(
            session_id=action.session_id,
            kv_cache_runtime=get_kv_cache_runtime(),
        )
        method = (
            session.suspend_kvc if action.action == "offload" else session.prepare_kvc
        )
        await method()

    task = asyncio.create_task(
        _dispatch(),
        name=f"product-kvc-{action.action}[{action.session_id}]",
    )
    _PRODUCT_GUARD_TASKS.add(task)
    task.add_done_callback(_PRODUCT_GUARD_TASKS.discard)
    task.add_done_callback(_log_product_guard_task)


def _log_product_guard_task(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("[ProductKVCacheHooks] task-guard action failed: %s", exc)
