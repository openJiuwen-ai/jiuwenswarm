# coding: utf-8
"""Bind interaction-loop continuation rounds to TaskPlan pending tasks.

openjiuwen ``DeepAgent._execute_round`` submits every round with a random
uuid task id, so the executor never marks TaskPlan tasks and the outer loop
spins forever on ``_has_remaining_tasks`` — holding the output lease, which
makes every new user message return an empty instant completion. This patch
restores the task-id resolution the non-interaction path already does.

Occupancy self-healing: cancelling a bound round during its SUBMITTED window
(scheduler has not picked it up yet) marks the CoreTask CANCELED without
running the executor, so the TaskPlan item stays PENDING while TaskManager
never removes the terminal CoreTask. Re-binding that id would make
``add_task`` raise "already exists!" on every continuation round — an
unhealable error-round spin. When the resolver meets such a terminal
occupant it marks the plan item cancelled (same semantics as the executor's
WORKING-cancel path) and moves on to the next pending item, so the loop
converges and the session self-heals.
"""
from __future__ import annotations

from typing import Any, Optional

from openjiuwen.core.common.logging import logger

__all__ = [
    "apply_deepagent_task_plan_binding_patch",
    "remove_deepagent_task_plan_binding_patch",
]

_PATCHED = False
_original_run_one_round: Any = None


async def _lookup_core_task(agent: Any, task_id: str) -> Optional[Any]:
    """Return the CoreTask occupying task_id in TaskManager, or None.

    Returns None when no task loop controller is bound yet (nothing can be
    occupied). Lookup errors propagate to the caller's fallback handling.
    """
    controller = getattr(agent, "loop_controller", None)
    task_manager = (
        getattr(controller, "task_manager", None)
        if controller is not None
        else None
    )
    if task_manager is None:
        return None

    from openjiuwen.core.controller.modules.task_manager import TaskFilter

    tasks = await task_manager.get_task(TaskFilter(task_id=task_id))
    return tasks[0] if tasks else None


def _is_terminal_core_task(core_task: Any) -> bool:
    """Whether the occupying CoreTask is in a terminal status.

    Terminal tasks are never removed from TaskManager and can never be
    re-registered (add_task raises "already exists!").
    """
    from openjiuwen.core.controller.schema.task import TaskStatus

    return getattr(core_task, "status", None) in (
        TaskStatus.CANCELED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    )


async def _resolve_plan_task_id(agent: Any, session: Any, work: Any) -> Optional[str]:
    """Return the TaskPlan's next pending task id for a continuation round."""
    if getattr(work, "kind", None) != "user":
        return None
    if getattr(work, "is_follow_up", False):
        return None
    if getattr(work, "reset_loop", True):
        return None
    if session is None:
        return None
    try:
        state = agent.load_state(session)
        plan = getattr(state, "task_plan", None)
        if plan is None:
            return None
        # Bounded: each iteration either returns or cancels one plan item.
        for _ in range(max(len(getattr(plan, "tasks", []) or []), 1)):
            next_task = plan.get_next_task()
            if next_task is None:
                return None
            occupied = await _lookup_core_task(agent, next_task.id)
            if occupied is None:
                return next_task.id
            if _is_terminal_core_task(occupied):
                # Zombie CoreTask (e.g. cancel during the SUBMITTED window):
                # the id can never be re-registered, so this plan item can
                # never run. Mark it cancelled to unblock the loop, then try
                # the next pending item.
                plan.mark_cancelled(
                    next_task.id,
                    reason=(
                        "core task already in terminal status "
                        f"({getattr(occupied, 'status', None)}); "
                        "round was cancelled before start"
                    ),
                )
                agent.save_state(session, state)
                logger.warning(
                    "[deepagent-plan-binding] plan task %s occupied by terminal "
                    "core task (status=%s session=%s); marked cancelled, "
                    "resolving next pending task",
                    next_task.id,
                    getattr(occupied, "status", None),
                    getattr(session, "get_session_id", lambda: None)(),
                )
                continue
            # SUBMITTED/WORKING/... occupation: an in-flight round owns this
            # id and will mark the plan item itself; fall back to raw task_id.
            logger.warning(
                "[deepagent-plan-binding] plan task %s occupied by in-flight "
                "core task (status=%s session=%s); fallback to raw task_id",
                next_task.id,
                getattr(occupied, "status", None),
                getattr(session, "get_session_id", lambda: None)(),
            )
            return None
        return None
    except Exception:
        logger.warning(
            "[deepagent-plan-binding] resolve plan task failed, fallback to raw task_id",
            exc_info=True,
        )
        return None


def _make_patched_run_one_round(original: Any) -> Any:
    async def _run_one_round_bound_to_plan(self: Any, work: Any, task_id: str, session: Any) -> Any:
        resolved = await _resolve_plan_task_id(self, session, work)
        if resolved and resolved != task_id:
            # getattr：跨类读受保护成员不触发 protected-access（同 mcp_call_timeout_patch 惯例）
            active = getattr(self, "_active_interaction_round", None)
            if active is not None and active.work is work:
                # 保持取消定向与实际注册的调度任务 id 一致
                active.task_id = resolved
            task_id = resolved
        return await original(self, work, task_id, session)

    return _run_one_round_bound_to_plan


def apply_deepagent_task_plan_binding_patch() -> None:
    """Bind interaction continuation rounds to TaskPlan tasks. Idempotent."""
    global _PATCHED, _original_run_one_round
    if _PATCHED:
        return
    _PATCHED = True

    from openjiuwen.harness.deep_agent import DeepAgent

    _original_run_one_round = DeepAgent.run_one_round
    setattr(
        DeepAgent,
        "run_one_round",
        _make_patched_run_one_round(_original_run_one_round),
    )
    logger.info(
        "[deepagent-plan-binding] patch applied "
        "(interaction continuation rounds bind to TaskPlan pending tasks)",
    )


def remove_deepagent_task_plan_binding_patch() -> None:
    """Undo the patch. Test-only; production never removes it."""
    global _PATCHED, _original_run_one_round
    if not _PATCHED or _original_run_one_round is None:
        return
    from openjiuwen.harness.deep_agent import DeepAgent

    setattr(DeepAgent, "run_one_round", _original_run_one_round)
    _original_run_one_round = None
    _PATCHED = False
