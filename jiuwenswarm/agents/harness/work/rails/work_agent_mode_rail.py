# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WorkAgentModeRail — work profile 的 plan 模式约束。

plan 的机械部分（进入/退出、计划文件、只读白名单、switch_mode 拦截、
拒绝后不误恢复模式）与 code profile 完全一致，因此直接复用
:class:`CodeAgentModeRail` 的实现，只替换构造参数：

- 工具白名单换成 work 场景的只读集合（无代码型子 agent）。
- 提示词换成通用工作版，不再要求调用 explore_agent / plan_agent。

这样 work 与 code 共享同一套安全兜底，不会因为两份拷贝而出现行为漂移。
"""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import CodeAgentModeRail
from jiuwenswarm.agents.harness.work.prompt.work_plan_prompts import (
    WORK_PLAN_ALLOWED_TOOLS,
    work_enter_plan_instructions,
    work_exit_plan_notification,
    work_plan_mode_system_note,
)

# 非 plan 态额外隐藏的工具。这个 rail 现在是常挂的（见 ``_build_agent_rails``），
# 而 ``AgentModeRail.init`` 会注册 switch_mode / enter_plan_mode / exit_plan_mode
# 三个工具。父类只在非 plan 态隐藏后两个，plan 态则按 ``WORK_PLAN_ALLOWED_TOOLS``
# 白名单过滤（其中不含 switch_mode）。也就是说只剩"非 plan 态的 switch_mode"这一
# 个缺口：不补上，TUI / IM / cron 的普通 work agent 会平白多出一个工具，还可能自己
# 切进 plan——work 的 plan 开关是用户在界面上控制、由服务端写进会话状态的。
_HIDDEN_IN_NORMAL_EXTRA = frozenset({"switch_mode"})


class WorkAgentModeRail(CodeAgentModeRail):
    """work profile 的 plan 模式 rail。

    Args:
        language: 提示词语言（``cn`` / ``en``）。
        allowed_tools: 覆盖默认的 work plan 工具白名单（测试与定制用）。
        exit_plan_notification: 覆盖退出 plan 后追加的提示。集群 Leader 需要
            被提醒改用团队工具，而不是自己直接执行。
    """

    def __init__(
        self,
        *,
        language: str = "cn",
        allowed_tools: list[str] | None = None,
        exit_plan_notification: str | None = None,
    ) -> None:
        super().__init__(
            allowed_tools=list(allowed_tools or WORK_PLAN_ALLOWED_TOOLS),
            plan_mode_attachment_note=work_plan_mode_system_note(language),
            enter_plan_instructions=work_enter_plan_instructions(language),
            exit_plan_notification=(
                exit_plan_notification or work_exit_plan_notification(language)
            ),
        )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """在父类基础上，非 plan 态再隐藏 ``switch_mode``。"""
        await super().before_model_call(ctx)
        if self._agent.load_state(ctx.session).plan_mode.mode == "plan":
            return
        if not isinstance(ctx.inputs.tools, list):
            return
        ctx.inputs.tools = [
            tool
            for tool in ctx.inputs.tools
            if getattr(tool, "name", "") not in _HIDDEN_IN_NORMAL_EXTRA
        ]


__all__ = ["WorkAgentModeRail"]
