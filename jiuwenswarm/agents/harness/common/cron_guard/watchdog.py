# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""L-WD: request-level hard deadline watchdog (issue #5018, design §3.2).

Wired at the AgentServer stream entry (``process_message_stream``).  Three
units per scheduled run:

1. **Soft timer** — ``loop.call_at(deadline_soft)`` sets
   ``ctx.soft_deadline_reached``; the CronBudgetRail consumes it on the next
   model call and requests a graceful force-finish.
2. **Hard deadline** — the producer coroutine is wrapped in
   ``asyncio.timeout_at(deadline_hard)``; the CancelledError/TimeoutError
   propagates into whatever await the agent is blocked on (model call,
   subprocess wait, MCP call) — the only real-time bound on an in-flight
   blocking tool batch.
3. **Reaper** — ``finally`` runs ``RunRegistry.reap(run_id)`` (process
   groups SIGTERM → grace → SIGKILL), writes the terminal ledger state, and
   unregisters the run.

All entry points are fail-open: any guard-internal error leaves the request
running as if the guard were absent.
"""

from __future__ import annotations

import time
from typing import Any

from jiuwenswarm.common.utils import logger

from .identity import (
    CronRunContext,
    current_cron_run,
    get_run_registry,
    resolve_cron_run_context,
)
from .ledger import STATE_FAILED, STATE_TRIPPED, get_run_ledger

_orphans_marked = False


class CronDeadlineExceeded(Exception):
    """Raised/queued when the hard deadline fires for a scheduled run."""


def cron_guard_begin(
    request: Any,
    session_id: str | None,
) -> tuple[CronRunContext | None, Any, Any]:
    """Resolve identity, register the run, start the soft timer.

    Returns ``(ctx, contextvar_token, soft_timer)`` — all ``None`` when the
    request is not a scheduled run (interactive: zero impact).

    Raises :class:`CronCheckpointQuarantined` only for an explicit L2
    quarantine-and-fail decision; every other failure is swallowed
    (fail-open) and returns ``(None, None, None)``.
    """
    from .checkpoint_guard import CronCheckpointQuarantined

    try:
        ctx = resolve_cron_run_context(
            getattr(request, "metadata", None),
            getattr(request, "params", None),
            session_id,
        )
    except CronCheckpointQuarantined:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cron_guard] identity resolution failed (fail-open): %s", exc)
        return None, None, None
    if ctx is None:
        return None, None, None

    from .config import get_cron_guard_config

    cfg = get_cron_guard_config()
    registry = get_run_registry()
    registry.register(ctx)
    try:
        # Cross-restart contract (design §3.3.2/§3.6): leftover ``running``
        # rows from a previous process become ``orphaned`` exactly once per
        # process, before this run's own row is inserted.
        global _orphans_marked
        ledger = get_run_ledger()
        if not _orphans_marked:
            n = ledger.mark_orphans()
            _orphans_marked = True
            if n:
                logger.warning(
                    "[cron_guard] marked %d leftover run(s) as orphaned at startup", n
                )
        ledger.start_run(ctx.run_id, ctx.job_id, ctx.sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cron_guard] ledger start failed (non-blocking): %s", exc)

    # L2 governance at entry (only with a trusted identity).
    if ctx.trusted and (cfg.get("checkpoint") or {}).get("enabled"):
        try:
            from .checkpoint_guard import govern_restore_at_entry

            govern_restore_at_entry(ctx)
        except CronCheckpointQuarantined:
            _finish(ctx, STATE_TRIPPED, "checkpoint_quarantined", reaped=False)
            # Roll the registration back: the request entry returns early on
            # this exception and never reaches its cron_guard_end cleanup, so
            # a leftover registry entry would pin the sid forever.
            registry.unregister(ctx.run_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] checkpoint governance failed (fail-open): %s", exc)

    token = current_cron_run.set(ctx)

    soft_timer = None
    if (cfg.get("deadline") or {}).get("enabled", True):
        try:
            import asyncio

            loop = asyncio.get_running_loop()

            def _soft_fire() -> None:
                ctx.soft_deadline_reached = True
                logger.info(
                    "[cron_guard] soft deadline reached run_id=%s (requesting graceful finish)",
                    ctx.run_id,
                )

            soft_timer = loop.call_at(ctx.deadline_soft, _soft_fire)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] soft timer setup failed (fail-open): %s", exc)

    logger.info(
        "[cron_guard] scheduled_run armed run_id=%s job_id=%s trusted=%s hard_in=%.0fs",
        ctx.run_id, ctx.job_id, ctx.trusted, ctx.deadline_hard - time.monotonic(),
    )
    return ctx, token, soft_timer


def cron_guard_end(
    ctx: CronRunContext | None,
    token: Any,
    soft_timer: Any,
    *,
    final_state: str = "finished",
    trip_reason: str | None = None,
) -> None:
    """Cleanup in the request ``finally``: timer, contextvar, reap, ledger, unregister.

    ``final_state``: ``finished`` for a clean run, ``tripped`` when the hard
    deadline or a budget guard terminated it, ``failed`` otherwise.
    """
    if ctx is None:
        return
    try:
        if soft_timer is not None:
            try:
                soft_timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        if token is not None:
            try:
                current_cron_run.reset(token)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[cron_guard] contextvar reset failed: %s", exc)

        cfg = None
        try:
            from .config import get_cron_guard_config

            cfg = get_cron_guard_config()
        except Exception:  # noqa: BLE001
            cfg = {}
        grace = float(((cfg or {}).get("deadline") or {}).get("kill_grace_seconds", 3))
        reaped = True
        try:
            reaped = get_run_registry().reap(ctx.run_id, kill_grace_seconds=grace)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cron_guard] reap raised (non-blocking): %s", exc)
            reaped = False

        _finish(ctx, final_state, trip_reason, reaped=reaped)
    finally:
        try:
            get_run_registry().unregister(ctx.run_id)
        except Exception:  # noqa: BLE001
            pass


def _finish(ctx: CronRunContext, state: str, trip_reason: str | None, reaped: bool) -> None:
    try:
        get_run_ledger().finish_run(
            ctx.run_id, state, trip_reason=trip_reason, reaped=reaped,
            snapshot=ctx.budget.snapshot(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cron_guard] ledger finish failed (non-blocking): %s", exc)
