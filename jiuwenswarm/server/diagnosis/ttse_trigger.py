# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hand diagnosed experience root causes to the Worker's resident TTSE pipeline.

A diagnosis only reports how a stretch of the task went (success, partial or
fail). Generating the FACT / TIP rules and saving them is TTSE's own job: the
reported outcome takes the place of TTSE's success detection, and the rail then
runs its usual induction (blame, synthesize, induce, dedupe, save) on the
supplied conversation. This module feeds that induction and never writes the
bank.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

MAX_SIGNALS = 50
_DEFAULT_SUCCESS_THRESHOLD = 0.7
_background_tasks: set[asyncio.Task[None]] = set()


def _result(result: str, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"result": result}
    if message:
        payload["message"] = message
    return payload


def _is_agent_work_session(session_id: str) -> bool | None:
    """Return whether the session is ``agent.work``; ``None`` when unknown."""
    try:
        from jiuwenswarm.common.mode_matrix import (
            canonicalize_mode_text,
            is_code_profile_mode,
            is_single_agent_mode,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
    except Exception:  # noqa: BLE001
        return None
    if not metadata:
        return None
    canonical = canonicalize_mode_text(metadata.get("mode"))
    team_name = str(metadata.get("team_name") or "").strip()
    return is_single_agent_mode(canonical) and not is_code_profile_mode(canonical) and not team_name


def _resident_adapter(agent: Any, session_id: str) -> Any:
    """Return the adapter that owns this session's runtime.

    The channel agent keeps one child adapter per session, and TTSE is mounted
    on that child. The channel adapter itself usually has no rail.
    """
    adapter = getattr(agent, "_adapter", None)
    if adapter is None:
        return None
    getter = getattr(adapter, "_get_cached_session_adapter", None)
    if not callable(getter):
        return adapter
    try:
        child = getter(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("[diagnosis] session adapter lookup failed: session_id=%s", session_id, exc_info=True)
        return adapter
    return child if child is not None else adapter


def _ttse_enabled_in_config() -> bool:
    """Return whether the saved config asks for TTSE to be mounted."""
    try:
        from jiuwenswarm.common.config import get_config, get_ttse_enabled

        return bool(get_ttse_enabled(get_config()))
    except Exception:  # noqa: BLE001
        logger.debug("[diagnosis] TTSE config lookup failed", exc_info=True)
        return False


async def _resolve_rail(agent: Any, session_id: str) -> Any:
    """Return this session's resident ``TTSERail``, mounting it when config asks.

    Turning the setting on only marks the session stale. The rail is normally
    built on the next chat turn, so a trigger that arrives first mounts it now.
    """
    adapter = _resident_adapter(agent, session_id)
    if adapter is None:
        return None
    rail = getattr(adapter, "_ttse_rail", None)
    if rail is not None:
        return rail
    if getattr(adapter, "_instance", None) is None or not _ttse_enabled_in_config():
        return None
    ensure = getattr(adapter, "_ensure_ttse_rail_registered", None)
    if not callable(ensure):
        return None
    try:
        await ensure()
    except Exception:  # noqa: BLE001
        logger.warning("[diagnosis] TTSE rail mount failed: session_id=%s", session_id, exc_info=True)
        return None
    return getattr(adapter, "_ttse_rail", None)


def explicit_score(outcome: str, score: float, threshold: float) -> float:
    """Map a signal onto the explicit task score TTSE classifies by threshold.

    TTSE reads ``0`` as fail, a value in ``(0, threshold)`` as partial and
    anything at or above the threshold as success, so the diagnosed outcome is
    kept authoritative and the score is adjusted into the matching band.

    Args:
        outcome: ``success``, ``partial`` or ``fail``.
        score: Score that belongs to the outcome, in [0, 1].
        threshold: TTSE success threshold.

    Returns:
        The score to hand to TTSE.
    """
    if outcome == "fail":
        return 0.0
    if outcome == "success":
        return max(score, threshold)
    if 0.0 < score < threshold:
        return score
    return threshold / 2


async def _run_induction(rail: Any, jobs: Sequence[Mapping[str, Any]]) -> None:
    # agent-core exposes no public entry for running induction on a supplied
    # conversation, so the rail's induction coroutine is looked up by name.
    # ``asyncio.CancelledError`` is a ``BaseException`` and is not caught below.
    run_induction = getattr(rail, "_run_ttse_induction", None)
    if not callable(run_induction):
        logger.warning("[diagnosis] TTSE rail has no induction entry; %d job(s) skipped", len(jobs))
        return
    for job in jobs:
        try:
            await run_induction(None, ctx=None, snapshot=dict(job["snapshot"]))
        except Exception:  # noqa: BLE001
            logger.warning("[diagnosis] TTSE induction failed: id=%s", job.get("id"), exc_info=True)


async def trigger_ttse_induction(
    *,
    agent_manager: Any,
    channel_id: str,
    session_id: str,
    signals: Any,
) -> dict[str, Any]:
    """Start TTSE induction for diagnosed experience root causes through the resident rail.

    The induction itself (several LLM calls under the bank lock) runs in the
    background, so a result of ``triggered`` only means TTSE accepted the work.

    Args:
        agent_manager: The Worker's ``AgentManager``.
        channel_id: Channel that owns the session runtime.
        session_id: Diagnosed session.
        signals: Payload list built by the diagnosis service; each item has
            ``id`` (the root cause id), ``outcome``, ``score``, ``task_query``
            and ``messages``.

    Returns:
        ``{"ok": True, "payload": {"results": ...}}`` or an error mapping
        ``{"ok": False, "error": ..., "code": ...}``.
    """
    if not isinstance(signals, list) or not signals:
        return {"ok": False, "error": "signals must be a non-empty list", "code": "BAD_REQUEST"}
    if _is_agent_work_session(session_id) is False:
        return {
            "ok": False,
            "error": "this session mode does not support TTSE",
            "code": "TTSE_NOT_SUPPORTED",
        }
    agent = None
    if agent_manager is not None:
        try:
            agent = agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id or "default",
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[diagnosis] agent lookup failed: session_id=%s", session_id, exc_info=True)
    rail = await _resolve_rail(agent, session_id)
    if rail is None:
        return {"ok": False, "error": "TTSE is not enabled for this session", "code": "TTSE_UNAVAILABLE"}
    config = getattr(rail, "_ttse_config", None)
    if not bool(getattr(config, "evolve_enabled", True)):
        return {"ok": False, "error": "TTSE evolution is disabled", "code": "TTSE_UNAVAILABLE"}
    threshold = float(getattr(config, "success_threshold", _DEFAULT_SUCCESS_THRESHOLD))
    capabilities = str(getattr(rail, "_last_capabilities", "") or "")

    results: dict[str, dict[str, Any]] = {}
    jobs: list[dict[str, Any]] = []
    for item in signals[:MAX_SIGNALS]:
        if not isinstance(item, Mapping):
            continue
        signal_id = str(item.get("id") or "")
        messages = item.get("messages")
        if not signal_id:
            continue
        if not isinstance(messages, list) or not messages:
            results[signal_id] = _result("failed", "no conversation to learn from")
            continue
        score = explicit_score(str(item.get("outcome") or ""), float(item.get("score") or 0.0), threshold)
        jobs.append(
            {
                "id": signal_id,
                "snapshot": {
                    "messages": messages,
                    "ttse_task_query": str(item.get("task_query") or ""),
                    "ttse_score": score,
                    "ttse_capabilities": capabilities,
                },
            },
        )
        results[signal_id] = _result("triggered")
    if jobs:
        task = asyncio.create_task(_run_induction(rail, jobs), name=f"diagnosis-ttse-{session_id}")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return {"ok": True, "payload": {"results": results}}


__all__ = ["explicit_score", "trigger_ttse_induction"]
