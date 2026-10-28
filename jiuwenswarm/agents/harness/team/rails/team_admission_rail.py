# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-wide Team LLM admission for non-Swarmflow (inprocess) runs.

When ``enable_swarmflow`` is false, Swarmflow's L1/L2/L3 governor does not
apply. Multiple teammates can still hit the model concurrently and stack RSS /
API quota. This rail adds a process-level ``asyncio.Semaphore`` around each
member's ``before_model_call`` / ``after_model_call`` so peak concurrent LLM
calls stay within ``max_concurrent_llm_calls``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_CTX_HOLD_KEY = "_team_llm_admission_hold"

_sem_guard = threading.Lock()
_sem: asyncio.Semaphore | None = None
_sem_loop: asyncio.AbstractEventLoop | None = None
_sem_limit: int | None = None


def _get_semaphore(limit: int) -> asyncio.Semaphore:
    """Return a process-wide semaphore sized to *limit*, rebound to the running loop."""
    global _sem, _sem_loop, _sem_limit
    loop = asyncio.get_running_loop()
    with _sem_guard:
        if _sem is None or _sem_loop is not loop or _sem_limit != limit:
            _sem = asyncio.Semaphore(max(1, int(limit)))
            _sem_loop = loop
            _sem_limit = int(limit)
            logger.info(
                "[TeamAdmission] process LLM semaphore ready limit=%s loop=%s",
                _sem_limit,
                id(loop),
            )
        return _sem


def reset_team_llm_semaphore_for_tests() -> None:
    """Clear the process-wide semaphore (unit tests only)."""
    global _sem, _sem_loop, _sem_limit
    with _sem_guard:
        _sem = None
        _sem_loop = None
        _sem_limit = None


class _PermitHold:
    """Idempotent lease on one Semaphore permit.

    Bound to the acquired Semaphore instance (not the process-global ``_sem``)
    so limit/loop rebuild cannot free the wrong object or leak the old one.

    Also registered on ``asyncio.Task.add_done_callback``: openjiuwen rail base
    skips ``after_model_call`` / ``on_model_exception`` when
    ``CancelledError`` is active (BaseException + ``is_cancelled`` finally
    gate), and cancel paths often never reach ``after_invoke``. Task completion
    is the reliable reclaim hook for chat.interrupt / cancel_session_runtime.
    """

    __slots__ = ("_sem", "_released")

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    @property
    def sem(self) -> asyncio.Semaphore:
        return self._sem

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._sem.release()
            logger.debug("[TeamAdmission] released LLM slot")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TeamAdmission] release failed: %s", exc)

    def on_task_done(self, _task: asyncio.Task) -> None:
        if not self._released:
            logger.debug(
                "[TeamAdmission] releasing LLM slot on task done (cancel safety net)"
            )
        try:
            self.release()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TeamAdmission] task-done release failed: %s", exc)


class TeamAdmissionRail(DeepAgentRail):
    """Gate Team member LLM calls behind a process-wide concurrency ceiling.

    Priority sits below overflow recovery (100) and above typical stream rails
    so admission runs early but recovery can still fire on model exceptions.
    """

    priority = 95

    def __init__(self, *, max_concurrent_llm_calls: int = 4) -> None:
        super().__init__()
        self._limit = max(1, int(max_concurrent_llm_calls))

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
            try:
                ctx.extra = extra
            except Exception:  # noqa: BLE001
                return
        existing = extra.get(_CTX_HOLD_KEY)
        if isinstance(existing, _PermitHold) and not existing.released:
            return
        sem = _get_semaphore(self._limit)
        # If acquire is cancelled while waiting, the permit was never taken.
        await sem.acquire()
        # Bind the acquired instance + task-done reclaim so CancelledError
        # during the in-flight model call cannot permanently leak the permit.
        hold = _PermitHold(sem)
        try:
            extra[_CTX_HOLD_KEY] = hold
            task = asyncio.current_task()
            if task is not None:
                task.add_done_callback(hold.on_task_done)
        except BaseException:
            hold.release()
            raise
        logger.debug(
            "[TeamAdmission] acquired LLM slot limit=%s",
            self._limit,
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        self._release(ctx)

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        self._release(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        # Invoke-level belt when after_model_call was skipped for non-cancel
        # reasons; cancel itself is primarily covered by _PermitHold.on_task_done.
        self._release(ctx)

    def _release(self, ctx: AgentCallbackContext) -> None:
        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, dict):
            return
        hold = extra.pop(_CTX_HOLD_KEY, None)
        if isinstance(hold, _PermitHold):
            hold.release()


def _match_modes_team_by_key(
    team: dict[str, Any],
    requested: str,
) -> dict[str, Any] | None:
    """Resolve ``modes.team`` entry by template key or session-scoped runtime name.

    Prefers the longest matching ``modes.team`` key so ``oc_team_foo_sess_x``
    binds to ``oc_team_foo`` rather than a shorter ``oc_team`` prefix.
    Also matches nested ``team_name`` fields the same way.
    """
    exact = team.get(requested)
    if isinstance(exact, dict):
        return exact

    best_len = -1
    best: dict[str, Any] | None = None
    for key, value in team.items():
        if not isinstance(value, dict):
            continue
        candidates = {str(key)}
        nested_name = str(value.get("team_name") or "").strip()
        if nested_name:
            candidates.add(nested_name)
        for name in candidates:
            if requested == name or requested.startswith(f"{name}_"):
                if len(name) > best_len:
                    best_len = len(name)
                    best = value
    return best


def resolve_team_section_for_admission(
    config: dict[str, Any] | None,
    *,
    team_key: str | None = None,
) -> dict[str, Any]:
    """Pick the active team dict for ``resource_admission`` / ``enable_swarmflow``.

    Accepts either a flat team section or a full config with ``modes.team``.
    When ``modes.team`` has multiple entries, selection is by explicit
    ``team_key`` (template id or session-scoped runtime name) — never by
    dict iteration order. Shared by runtime inheritance and swarm config
    assembly so the two paths cannot drift.
    """
    if not isinstance(config, dict):
        return {}
    # Spec-shaped / already-resolved team section.
    if "enable_swarmflow" in config or "resource_admission" in config:
        return config
    modes = config.get("modes")
    if not isinstance(modes, dict):
        return config
    team = modes.get("team")
    if not isinstance(team, dict) or not team:
        return config

    requested = str(team_key or "").strip()
    if not requested:
        # Blueprint often carries team_name after load_team_spec_dict.
        requested = str(config.get("team_name") or "").strip()

    if requested:
        matched = _match_modes_team_by_key(team, requested)
        if matched is not None:
            return matched
        logger.warning(
            "[TeamAdmission] modes.team has no entry for team_key=%r; keys=%s",
            requested,
            [k for k, v in team.items() if isinstance(v, dict)],
        )

    # Product default when callers omit team_key (single-template installs).
    nested = team.get("jiuwen_team")
    if isinstance(nested, dict):
        return nested

    dict_entries = [(k, v) for k, v in team.items() if isinstance(v, dict)]
    if len(dict_entries) == 1:
        return dict_entries[0][1]
    if dict_entries:
        logger.warning(
            "[TeamAdmission] ambiguous modes.team entries=%s without usable "
            "team_key=%r; refusing first-dict heuristic",
            [k for k, _ in dict_entries],
            requested or None,
        )
        return {}
    return config


def resolve_team_admission_limit(team_cfg: dict[str, Any] | None) -> int:
    """Return configured LLM admission limit; ``0`` means disabled.

    Reads ``resource_admission.max_concurrent_llm_calls``. When Swarmflow is
    enabled the default is ``0`` (governor already caps agents); when Swarmflow
    is off the default is ``4``.
    """
    if not isinstance(team_cfg, dict):
        return 4
    admission = team_cfg.get("resource_admission")
    if not isinstance(admission, dict):
        admission = {}
    enable_swarmflow = bool(team_cfg.get("enable_swarmflow", False))
    default = 0 if enable_swarmflow else 4
    raw = admission.get("max_concurrent_llm_calls")
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        # YAML `.inf` → float('inf'); int(inf) raises OverflowError (nan → ValueError).
        logger.warning(
            "[TeamAdmission] invalid max_concurrent_llm_calls=%r; falling back to default=%s",
            raw,
            default,
        )
        return default


def build_team_admission_rail(
    team_cfg: dict[str, Any] | None = None,
    *,
    max_concurrent_llm_calls: int | None = None,
) -> Optional[TeamAdmissionRail]:
    """Build ``TeamAdmissionRail`` when limit > 0; otherwise return ``None``."""
    limit = (
        max_concurrent_llm_calls
        if max_concurrent_llm_calls is not None
        else resolve_team_admission_limit(team_cfg)
    )
    if limit <= 0:
        return None
    try:
        return TeamAdmissionRail(max_concurrent_llm_calls=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TeamAdmission] rail create failed: %s", exc, exc_info=True)
        return None
