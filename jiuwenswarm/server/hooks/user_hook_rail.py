# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""UserHookRail —— 将用户配置的 hooks 以 Rail 形态注册到 DeepAgent，拦截工具调用和 Agent 生命周期."""

from __future__ import annotations

import logging
from contextvars import ContextVar

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.ability_manager import resolve_tool_message
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, RunContext
from openjiuwen.harness.goal.schema import GoalAssessment, GoalAssessmentStatus
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.hooks_config import HooksConfig, HookEvent
from jiuwenswarm.server.hooks.executor import HookExecutor

logger = logging.getLogger(__name__)

_PENDING_GOAL_KEY = "_user_hook_pending_goal"


class UserHookRail(DeepAgentRail):
    """Execution engine for user-configured hooks.

    Priority 60 runs after the security rails. ``JiuSwarmStreamEventRail``
    projects a tool call only after every rail that rewrites its result, so
    PostToolUse context added here is part of the streamed ``rendered_result``.
    """

    priority = 60

    def __init__(self, hooks_config: HooksConfig):
        super().__init__()
        self._config = hooks_config
        self._executor = HookExecutor()
        self._blocking_state: ContextVar[dict | None] = ContextVar("user_hook_blocking_state", default=None)

    @staticmethod
    def _session_id(ctx: AgentCallbackContext) -> str:
        session = getattr(ctx, "session", None)
        if session is None:
            return ""
        get_session_id = getattr(session, "get_session_id", None)
        return get_session_id() if callable(get_session_id) else ""

    # ---- PreToolUse: BEFORE_TOOL_CALL ----

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        # HITL resumes execute tools before any model call. ContextVar also
        # bridges DeepAgent's outer callbacks to its inner ReAct/tool tasks,
        # while keeping concurrent invocations isolated.
        self._blocking_state.set({})
        if ctx.session is not None and isinstance(getattr(ctx.inputs, "query", None), InteractiveInput):
            # Approval work is a user round with no goal run_context. Keep the
            # interrupted generation, even if the goal changed while waiting.
            ctx.extra[_PENDING_GOAL_KEY] = ctx.session.get_state(_PENDING_GOAL_KEY)

    async def before_task_iteration(self, ctx: AgentCallbackContext) -> None:
        self._blocking_state.set({})

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = ctx.inputs.tool_name or ""
        tool_args = ctx.inputs.tool_args
        blocking_state = self._blocking_state.get()
        if blocking_state is None:
            blocking_state = ctx.extra.setdefault("_hook_blocking_state", {})
        if blocking_state:
            self._finish_blocked_tool(ctx, blocking_state["reason"])
            return

        hook_configs = self._config.match(
            HookEvent.PRE_TOOL_USE.value, query=tool_name,
        )
        if not hook_configs:
            return

        results = await self._executor.run_all(
            hook_configs,
            hook_input={
                "event": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_args,
                "session_id": self._session_id(ctx),
            },
        )

        # Another tool's hook may have blocked while this hook was running.
        if blocking_state:
            self._finish_blocked_tool(ctx, blocking_state["reason"])
            return

        for r in results:
            if r.outcome == "blocking":
                blocking_state["reason"] = r.error
                self._finish_blocked_tool(ctx, r.error)
                return
            if r.modified_input:
                ctx.inputs.tool_args = r.modified_input
                new_name = r.modified_input.get("_tool_name")
                if new_name:
                    ctx.inputs.tool_name = new_name
                logger.info(
                    "UserHookRail: PreToolUse modified input for tool=%s", tool_name,
                )
            if r.additional_context:
                existing = ctx.extra.get("_hook_additional_context", "")
                ctx.extra["_hook_additional_context"] = existing + "\n" + r.additional_context

    @staticmethod
    def _finish_blocked_tool(ctx: AgentCallbackContext, reason: str) -> None:
        tool_name = ctx.inputs.tool_name or ""
        ctx.extra["_skip_tool"] = True
        ctx.extra["_hook_feedback"] = reason
        feedback = f"[Hook blocked] PreToolUse blocked {tool_name}: {reason}"
        ctx.inputs.tool_result = {
            "success": False,
            "status": "blocked",
            "reason": reason,
            "retryable": False,
        }
        ctx.inputs.tool_msg = ToolMessage(content=feedback, tool_call_id=ctx.inputs.tool_call.id)
        # Skipping one tool does not stop ReAct from asking the model to retry.
        ctx.request_force_finish({
            "output": feedback,
            "result_type": "answer",
            "stop_reason": "hook_blocked",
        })
        logger.info("UserHookRail: PreToolUse BLOCKED tool=%s reason=%s", tool_name, reason)

    # ---- PostToolUse: AFTER_TOOL_CALL ----

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        # The framework fires AFTER_TOOL_CALL even when PreToolUse skipped it.
        if "_hook_feedback" in ctx.extra:
            return
        tool_name = ctx.inputs.tool_name or ""

        hook_configs = self._config.match(
            HookEvent.POST_TOOL_USE.value, query=tool_name,
        )
        if not hook_configs:
            return

        results = await self._executor.run_all(
            hook_configs,
            hook_input={
                "event": "PostToolUse",
                "tool_name": tool_name,
                "tool_input": ctx.inputs.tool_args,
                "tool_result": ctx.inputs.tool_result,
                "session_id": self._session_id(ctx),
            },
        )

        for r in results:
            if r.outcome == "blocking":
                ctx.extra["_post_tool_hook_feedback"] = r.error
                logger.info(
                    "UserHookRail: PostToolUse BLOCKED continuation tool=%s reason=%s",
                    tool_name, r.error,
                )
            if r.additional_context:
                self._append_model_context(ctx, r.additional_context)

    @staticmethod
    def _append_model_context(ctx: AgentCallbackContext, additional_context: str) -> None:
        """Append PostToolUse context to the tool message the model reads.

        The model reads the tool message, not ``tool_result``, and the
        structured result stays untouched for program consumers. A call that
        raised carries its message on the execution error.
        """
        message = resolve_tool_message(ctx.inputs, ctx.exception)
        if message is None or not isinstance(message.content, str):
            logger.warning(
                "UserHookRail: no tool message to attach PostToolUse context to, tool=%s",
                ctx.inputs.tool_name,
            )
            return
        message.content = message.content + "\n[Hook 发现]: " + additional_context

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
        """Keep pending tasks from automatically restarting a blocked round."""
        await self._handle_round_result(ctx)

    @staticmethod
    def _goal_attempt(ctx: AgentCallbackContext) -> dict | None:
        run_kind = getattr(ctx.inputs, "run_kind", None)
        if getattr(run_kind, "value", run_kind) != "goal":
            return ctx.extra.get(_PENDING_GOAL_KEY)
        run_context = ctx.inputs.run_context
        if isinstance(run_context, RunContext):
            run_context = run_context.extra
        elif isinstance(run_context, dict):
            run_context = {**run_context, **(run_context.get("extra") or {})}
        else:
            return None
        goal_id, revision = run_context.get("goal_id"), run_context.get("revision")
        if goal_id is None or revision is None:
            return None
        return {"goal_id": goal_id, "revision": revision}

    async def _handle_round_result(self, ctx: AgentCallbackContext) -> None:
        result = getattr(ctx.inputs, "result", None)
        if not isinstance(result, dict):
            return
        goal = self._goal_attempt(ctx)
        if ctx.session is not None:
            # Store on the session so a rail reload does not lose ownership.
            # Repeated interruptions retain it; any final result clears it.
            ctx.session.update_state({
                _PENDING_GOAL_KEY: goal if result.get("result_type") == "interrupt" else None,
            })
        if result.get("stop_reason") != "hook_blocked":
            return
        coordinator = getattr(ctx.agent, "loop_coordinator", None)
        if coordinator is not None:
            coordinator.request_abort()
        manager = getattr(ctx.agent, "goal_manager", None)
        if manager is None or goal is None:
            return
        # Run before TaskCompletionRail (priority 10). A terminal assessment
        # prevents both completion evaluation and the scheduler from retrying.
        # The manager rejects stale results from a replaced/resumed goal.
        record = await manager.apply_assessment(
            goal_id=goal["goal_id"],
            revision=goal["revision"],
            assessment=GoalAssessment(
                status=GoalAssessmentStatus.BLOCKED,
                evidence=result["output"],
                next_instruction="Resolve the PreToolUse hook block before explicitly resuming the goal.",
            ),
        )
        if record is not None:
            ctx.agent.event_manager.discard_goal_work(session_id=record.session_id, goal_id=record.goal_id)

    # ---- PostToolUseFailure: ON_TOOL_EXCEPTION ----

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        tool_name = ctx.inputs.tool_name or ""

        hook_configs = self._config.match(
            HookEvent.POST_TOOL_USE_FAILURE.value, query=tool_name,
        )
        if not hook_configs:
            return

        await self._executor.run_all(
            hook_configs,
            hook_input={
                "event": "PostToolUseFailure",
                "tool_name": tool_name,
                "tool_input": ctx.inputs.tool_args,
                "error": str(getattr(ctx, "exception", "")),
                "session_id": self._session_id(ctx),
            },
        )

    # ---- Stop: AFTER_INVOKE ----

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        # DeepAgent routes approvals directly to ReAct, without a task
        # iteration. Ordinary rounds were already handled there: applying
        # their result again could undo a user's concurrent goal resume.
        if isinstance(getattr(ctx.inputs, "query", None), InteractiveInput):
            await self._handle_round_result(ctx)
        hook_configs = self._config.match(HookEvent.STOP.value)
        if not hook_configs:
            return

        results = await self._executor.run_all(
            hook_configs,
            hook_input={
                "event": "Stop",
                "final_response": getattr(ctx.inputs, "result", None),
                "session_id": self._session_id(ctx),
            },
        )

        for r in results:
            if r.outcome == "blocking":
                ctx.extra["_stop_hook_feedback"] = r.error
                logger.info("UserHookRail: Stop hook feedback: %s", r.error[:200])
