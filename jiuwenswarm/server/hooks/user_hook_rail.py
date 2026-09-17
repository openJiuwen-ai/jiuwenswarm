# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""UserHookRail —— 将用户配置的 hooks 以 Rail 形态注册到 DeepAgent，拦截工具调用和 Agent 生命周期."""

from __future__ import annotations

import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.hooks_config import HooksConfig, HookEvent
from jiuwenswarm.server.hooks.executor import HookExecutor

logger = logging.getLogger(__name__)


class UserHookRail(DeepAgentRail):
    """用户配置的 hooks 执行引擎.

    Priority=60: 在 SecurityRail (80) 之后，JiuSwarmStreamEventRail (50) 之前。
    确保安全检查先于用户 hook，用户 hook 先于流式事件发送。
    """

    priority = 60

    def __init__(self, hooks_config: HooksConfig):
        super().__init__()
        self._config = hooks_config
        self._executor = HookExecutor()

    @staticmethod
    def _session_id(ctx: AgentCallbackContext) -> str:
        session = getattr(ctx, "session", None)
        if session is None:
            return ""
        get_session_id = getattr(session, "get_session_id", None)
        return get_session_id() if callable(get_session_id) else ""

    # ---- PreToolUse: BEFORE_TOOL_CALL ----

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = ctx.inputs.tool_name or ""
        tool_args = ctx.inputs.tool_args

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

        for r in results:
            if r.outcome == "blocking":
                ctx.extra["_skip_tool"] = True
                ctx.extra["_hook_feedback"] = r.error
                logger.info(
                    "UserHookRail: PreToolUse BLOCKED tool=%s reason=%s",
                    tool_name, r.error,
                )
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

    # ---- PostToolUse: AFTER_TOOL_CALL ----

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
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
                current = ctx.inputs.tool_result or ""
                ctx.inputs.tool_result = current + "\n[Hook 发现]: " + r.additional_context

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
