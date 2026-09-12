# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic delegation gate for graph-materialized expert teams.

The package prompt tells the leader to route every non-clarification request to
at least one expert member.  This rail turns that instruction into a runtime
contract: a leader answer cannot leave the process until a scheduled
``create_task`` call has actually succeeded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.single_agent.rail import (
    AgentCallbackContext,
    ModelCallInputs,
    TaskIterationInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails import DeepAgentRail

logger = logging.getLogger(__name__)

_SESSION_STATE_KEY = "xiaoyi.expert_team.delegation_gate"
_SCHEMA_VERSION = 1
_ORIGINAL_STREAMING_KEY = "_expert_team_delegation_original_streaming"
_TASK_REVIEW_FIELDS = frozenset(
    {
        "reviewer",
        "reviewers",
        "verifier",
        "verifiers",
        "max_review_rounds",
        "review_required",
        "requires_review",
        "verification_required",
    }
)

_RETRY_STEERING = (
    "[EXPERT_TEAM_DELEGATION_REQUIRED] 你是主理人，不是专业执行成员。"
    "本轮尚未成功委派任务：请立即调用 create_task，将至少一个任务指派给已注册的"
    "非主理人成员；不得直接输出方案、正文或成品。若确实缺少必要输入，只能调用"
    " ask_user 澄清。"
)
_FAILURE_OUTPUT = (
    "专家团调度未能启动：主理人在运行时纠正后仍未成功调用 create_task。"
    "本轮已安全终止，请重试。"
)


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "delegated": False,
        "violations": 0,
    }


def _session_id(ctx: AgentCallbackContext) -> str:
    session = ctx.session
    getter = getattr(session, "get_session_id", None)
    return str(getter() if callable(getter) else "")


def _load_state(ctx: AgentCallbackContext) -> dict[str, Any]:
    session = ctx.session
    getter = getattr(session, "get_state", None)
    if not callable(getter):
        return _default_state()
    state = getter(_SESSION_STATE_KEY)
    if not isinstance(state, Mapping) or state.get("schema_version") != _SCHEMA_VERSION:
        return _default_state()
    try:
        violations = max(0, int(state.get("violations", 0) or 0))
    except (TypeError, ValueError):
        violations = 0
    return {
        "schema_version": _SCHEMA_VERSION,
        "delegated": bool(state.get("delegated", False)),
        "violations": violations,
    }


def _save_state(ctx: AgentCallbackContext, state: Mapping[str, Any]) -> None:
    updater = getattr(ctx.session, "update_state", None)
    if callable(updater):
        updater({_SESSION_STATE_KEY: dict(state)})


def _response_tool_calls(response: Any) -> list[Any]:
    if isinstance(response, Mapping):
        value = response.get("tool_calls")
    else:
        value = getattr(response, "tool_calls", None)
    return list(value) if isinstance(value, (list, tuple)) else []


def _clear_response(response: Any) -> None:
    """Erase rejected model text before the ReAct loop can commit it."""
    if isinstance(response, dict):
        response["content"] = ""
        response["reasoning_content"] = ""
        return
    if response is None:
        return
    if hasattr(response, "content"):
        response.content = ""
    if hasattr(response, "reasoning_content"):
        response.reasoning_content = ""


def _tool_args_mapping(value: Any) -> Mapping[str, Any]:
    """Normalize AFTER_TOOL_CALL args from the runtime's raw JSON form."""
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _without_task_review_fields(value: Any) -> tuple[Any, int]:
    """Strip task-level review controls while preserving the input shape."""
    args = _tool_args_mapping(value)
    tasks = args.get("tasks")
    if not isinstance(tasks, list):
        return value, 0

    removed = 0
    clean_tasks: list[Any] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            clean_tasks.append(task)
            continue
        clean_task = dict(task)
        for field in _TASK_REVIEW_FIELDS:
            if field in clean_task:
                clean_task.pop(field)
                removed += 1
        clean_tasks.append(clean_task)
    if not removed:
        return value, 0

    clean_args = dict(args)
    clean_args["tasks"] = clean_tasks
    if isinstance(value, str):
        return json.dumps(clean_args, ensure_ascii=False), removed
    return clean_args, removed


def _canonical_create_result(value: Any, assignees: set[str]) -> bool:
    """Recognize create_task's stable model-facing success representation."""
    result = str(value or "").strip()
    if result.startswith("Task created:"):
        return "task_id=" in result and any(
            f"-> {assignee}" in result for assignee in assignees
        )
    lines = result.splitlines()
    return (
        len(lines) >= 2
        and lines[-1].startswith("Created ")
        and all(line.startswith("task_id=") for line in lines[:-1])
        and all(
            any(f"-> {assignee}" in line for assignee in assignees)
            for line in lines[:-1]
        )
    )


def _successful_scheduled_create(inputs: ToolCallInputs) -> bool:
    if inputs.tool_name != "create_task":
        return False
    output = inputs.tool_result
    declared_success = (
        output.get("success")
        if isinstance(output, Mapping)
        else getattr(output, "success", None)
    )

    args = _tool_args_mapping(inputs.tool_args)
    requested = args.get("tasks")
    requested_assignees = (
        {
            str(task.get("assignee") or "").strip()
            for task in requested
            if isinstance(task, Mapping)
            and str(task.get("assignee") or "").strip()
        }
        if isinstance(requested, list)
        else set()
    )
    if not requested_assignees:
        return False
    canonical_success = _canonical_create_result(output, requested_assignees)
    if declared_success is not True and not canonical_success:
        return False

    data = (
        output.get("data")
        if isinstance(output, Mapping)
        else getattr(output, "data", None)
    )
    if isinstance(data, Mapping):
        created = data.get("tasks") if isinstance(data.get("tasks"), list) else [data]
        return any(
            isinstance(task, Mapping)
            and str(task.get("task_id") or "").strip()
            and str(task.get("assignee") or "").strip() in requested_assignees
            for task in created
        )
    if isinstance(data, str):
        # beta3 persists successful team-tool results as this canonical text.
        # Keep ``success=True`` as the primary signal and additionally bind the
        # returned task marker to an assignee from the original request.
        return _canonical_create_result(data, requested_assignees)
    return canonical_success


class ExpertTeamDelegationGateRail(DeepAgentRail):
    """Fail closed until the scheduled leader delegates real member work."""

    # Run after ordinary prompt/model rails so no later callback can re-enable
    # streaming or restore rejected text.
    priority = 1

    def __init__(
        self,
        *,
        max_corrections: int = 2,
        task_manager: Any | None = None,
    ) -> None:
        super().__init__()
        self.max_corrections = max(0, int(max_corrections))
        self._task_manager = task_manager
        self._delegated_in_current_round = False
        self._allow_task_board_recovery = False

    async def _is_delegated(self, ctx: AgentCallbackContext) -> bool:
        """Resolve delegation from live state or a follow-up task-board fact.

        Team member completion resumes the leader in a follow-up round.  The
        native harness may rebuild the callback ``Session`` for that round, so
        the private session state written by ``after_tool_call`` is not a
        durable source of truth.  The scheduled team's task board is durable:
        a persisted task with a real non-leader assignee proves that a prior
        ``create_task`` succeeded.

        Recovery is intentionally enabled only by a continuation iteration:
        either an explicit follow-up or an outer-loop iteration after the first.
        A fresh user run starts at iteration 1, resets the gate, and cannot
        inherit an old task from the same chat.  This preserves the fail-closed
        first-delegation rule while covering scheduler continuations, which the
        beta3 task loop emits with ``is_follow_up=False`` and ``iteration>1``.
        """
        state = _load_state(ctx)
        if state["delegated"] or self._delegated_in_current_round:
            if not state["delegated"]:
                state["delegated"] = True
                _save_state(ctx, state)
            return True
        if not self._allow_task_board_recovery or self._task_manager is None:
            return False

        try:
            tasks = await self._task_manager.list_tasks()
        except Exception:  # noqa: BLE001 - unavailable evidence must fail closed
            logger.warning(
                "Expert-team delegation gate could not inspect the task board "
                "during follow-up recovery (session=%s)",
                _session_id(ctx),
                exc_info=True,
            )
            return False

        leader_name = str(
            getattr(self._task_manager, "member_name", "") or ""
        ).strip()
        for task in tasks or []:
            task_id_value = (
                task.get("task_id", "")
                if isinstance(task, Mapping)
                else getattr(task, "task_id", "")
            )
            assignee_value = (
                task.get("assignee", "")
                if isinstance(task, Mapping)
                else getattr(task, "assignee", "")
            )
            task_id = str(task_id_value or "").strip()
            assignee = str(assignee_value or "").strip()
            if not task_id or not assignee or (leader_name and assignee == leader_name):
                continue
            state["delegated"] = True
            _save_state(ctx, state)
            self._delegated_in_current_round = True
            logger.info(
                "Expert-team delegation gate recovered from persisted task "
                "(session=%s, task_id=%s, assignee=%s)",
                _session_id(ctx),
                task_id,
                assignee,
            )
            return True
        return False

    async def before_task_iteration(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, TaskIterationInputs):
            return
        if inputs.is_follow_up or inputs.iteration > 1:
            self._allow_task_board_recovery = True
            return
        self._delegated_in_current_round = False
        self._allow_task_board_recovery = False
        _save_state(ctx, _default_state())

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if await self._is_delegated(ctx):
            return
        ctx.extra.setdefault(
            _ORIGINAL_STREAMING_KEY,
            bool(ctx.extra.get("_streaming", False)),
        )
        # Rejected text must never be emitted as live llm_output chunks.
        ctx.extra["_streaming"] = False

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Keep v3 expert-team tasks out of the unavailable reviewer flow."""
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if ctx.inputs.tool_name != "create_task":
            return
        clean_args, removed = _without_task_review_fields(ctx.inputs.tool_args)
        if not removed:
            return
        ctx.inputs.tool_args = clean_args
        logger.info(
            "Expert-team delegation gate removed %d task review field(s) "
            "before create_task (session=%s)",
            removed,
            _session_id(ctx),
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ModelCallInputs):
            return
        if await self._is_delegated(ctx):
            return
        state = _load_state(ctx)
        response = ctx.inputs.response
        # Tool-bearing responses are allowed to execute.  Only a successful
        # scheduled create_task call unlocks the final answer in after_tool_call.
        if _response_tool_calls(response):
            return

        _clear_response(response)
        state["violations"] += 1
        _save_state(ctx, state)
        logger.warning(
            "Expert-team leader attempted direct output before delegation "
            "(session=%s, violation=%d)",
            _session_id(ctx),
            state["violations"],
        )
        if state["violations"] <= self.max_corrections and ctx.steering_queue is not None:
            ctx.push_steering(_RETRY_STEERING)
            return
        ctx.request_force_finish(
            {"output": _FAILURE_OUTPUT, "result_type": "error"}
        )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if not _successful_scheduled_create(ctx.inputs):
            return
        state = _load_state(ctx)
        state["delegated"] = True
        _save_state(ctx, state)
        self._delegated_in_current_round = True
        logger.info(
            "Expert-team delegation gate unlocked (session=%s)",
            _session_id(ctx),
        )
        original_streaming = ctx.extra.pop(_ORIGINAL_STREAMING_KEY, None)
        if original_streaming is not None:
            ctx.extra["_streaming"] = bool(original_streaming)


__all__ = ["ExpertTeamDelegationGateRail"]
