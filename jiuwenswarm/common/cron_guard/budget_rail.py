# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""L3/L4: cron iteration and wall-clock budget rail (issue #5018, design §3.5).

Mounted on every DeepAgent (cheap no-op for interactive requests: the rail
only acts when a cron run context is resolvable).  All counters live in the
shared :class:`RunBudget` — the rail itself is stateless per run and does
**not** depend on the upstream ``_react_iteration`` (multiple ``continue``
paths bypass the after-iteration event).

Two-level action at the limit (design §3.5):

1. ``ctx.request_force_finish(...)`` — ask the agent loop to finish
   gracefully with a summary result.
2. If force-finish cannot be consumed at this point, raise
   :class:`CronBudgetExceeded`, which the request entry converts into
   ``chat.error``.

The rail never touches interactive requests and fails open on its own
errors.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger


class CronBudgetExceeded(Exception):
    """Raised when a cron run exceeds its iteration/wall-clock budget hard stop."""


class CronBudgetRail(DeepAgentRail):
    """Iteration cap + tool-wait budget + soft-deadline finishing for cron runs.

    Registration goes through the adapter's rail build list
    (``_build_cron_budget_rail`` → ``_RailBuildInfo`` → ``register_rail``);
    the base ``get_callbacks`` discovers the overridden ``before_model_call``.
    """

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        try:
            from .config import get_cron_guard_config
            from .identity import current_cron_run

            cfg = get_cron_guard_config()
            if not cfg.get("enabled"):
                return
            # ContextVar identity with a session binding.  The registry's
            # session->run fallback exists for out-of-task consumers
            # (thread-pool shell-wait accounting); consulting it here let a
            # leaked or stale registration govern unrelated interactive
            # requests.  ContextVar alone is not enough either: an abandoned
            # entry generator (early return / cancelled stream skipping
            # cron_guard_end) can leave its context visible on a reused
            # worker — CI 10210's 242-failure permission cluster was exactly
            # that leak governing every later request once its small
            # RunBudget ran out.  Governance therefore requires BOTH the
            # ContextVar binding AND a matching session id: a context from
            # another session is treated as interactive (design §3.1).
            run = current_cron_run.get()
            if run is None:
                return  # interactive: zero impact
            sid = getattr(getattr(ctx, "session", None), "session_id", None)
            if not sid or run.sid != sid:
                return  # foreign/leaked cron context: never governs

            budget = run.budget
            calls = budget.add_model_call()
            from .ledger import sync_budget_to_ledger

            sync_budget_to_ledger(run)

            max_iterations = run.max_iterations
            # review root 200900675: idempotency guard — once a force finish
            # was requested, repeated over-limit model calls must not re-inject
            # the finish request or re-write the ledger trip (same semantics as
            # the soft-deadline branch below).
            if (
                max_iterations is not None
                and calls > int(max_iterations)
                and not run.force_finish_requested
            ):
                reason = f"max_iterations={max_iterations} (model_calls={calls})"
                self._finish_or_raise(ctx, run, reason)
                return

            if run.soft_deadline_reached and not run.force_finish_requested:
                self._finish_or_raise(ctx, run, "soft_deadline")
                return

            if budget.tool_wait_exceeded() and not run.force_finish_requested:
                # atomgit 2026-11-18：补 force_finish 幂等守卫——与迭代上限/
                # 软截止分支同判据，已消费 finish 后不得重复注入。
                reason = (
                    f"tool_wait_budget={budget.tool_wait_budget_seconds:g}s "
                    f"shell_seconds={budget.shell_seconds:g}s"
                )
                self._finish_or_raise(ctx, run, reason)
                return
        except CronBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — rail failure must not break the agent loop
            logger.warning("[cron_guard] budget rail error (fail-open): %s", exc)

    @staticmethod
    def _finish_or_raise(ctx: AgentCallbackContext, run: Any, reason: str) -> None:
        run.force_finish_requested = True
        logger.info(
            "[cron_guard] budget limit run_id=%s reason=%s — requesting force finish",
            run.run_id, reason,
        )
        try:
            # review root 200900676: the repo-wide force-finish payload
            # contract is ``output`` + ``result_type`` (see
            # jiuwenswarm/runtime/iteration_limit.py, circuit_breaker_rail,
            # code_agent_mode_rail).  A "content" key is not projected by the
            # engine → the forced finish would surface as an empty answer.
            ctx.request_force_finish({
                "output": (
                    "[cron_guard] 定时任务达到运行预算上限，已强制收尾。"
                    f"原因：{reason}。请基于已完成的工作汇报当前结果。"
                ),
                "result_type": "answer",
                "code": "CRON_BUDGET_EXCEEDED",
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[cron_guard] force_finish unavailable (%s) — raising CronBudgetExceeded",
                exc,
            )
            raise CronBudgetExceeded(f"cron run {run.run_id}: {reason}") from exc
