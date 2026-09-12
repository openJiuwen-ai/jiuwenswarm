# coding: utf-8
"""Bind interaction-loop continuation rounds to TaskPlan pending tasks.

openjiuwen ``DeepAgent._execute_round`` submits every round with a random
uuid task id, so the executor never marks TaskPlan tasks and the outer loop
spins forever on ``_has_remaining_tasks`` — holding the output lease, which
makes every new user message return an empty instant completion. This patch
restores the task-id resolution the non-interaction path already does.
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


def _resolve_plan_task_id(agent: Any, session: Any, work: Any) -> Optional[str]:
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
        next_task = plan.get_next_task()
        return next_task.id if next_task is not None else None
    except Exception:
        logger.warning(
            "[deepagent-plan-binding] resolve plan task failed, fallback to raw task_id",
            exc_info=True,
        )
        return None


def _make_patched_run_one_round(original: Any) -> Any:
    async def _run_one_round_bound_to_plan(self: Any, work: Any, task_id: str, session: Any) -> Any:
        resolved = _resolve_plan_task_id(self, session, work)
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
