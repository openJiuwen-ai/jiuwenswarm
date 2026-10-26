# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 主入口 -- 串联规划、执行、降级。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment
from jiuwenswarm.server.runtime.skill_turbo.executor import PlanCodeLoadError, SkillTurboExecutor
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidationError
from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentResponseChunk


class SkillTurboNotHandled(Exception):
    """SkillTurbo 无法处理该任务，需要降级到 DeepAgent。"""


class SkillTurbo:
    """SkillTurbo 主入口。"""

    def __init__(self, config: dict[str, Any]):
        self._env = SkillTurboEnvironment(config)
        self._planner = SkillTurboPlanner(self._env)
        self._executor = SkillTurboExecutor(self._env)

    @property
    def artifact_holder(self) -> dict[str, dict[str, Any]]:
        """返回 executor 的节点产物 holder，供外部构建产物摘要。"""
        return self._executor.node_artifacts

    def _sync_skill_name_to_inputs(self, inputs: dict[str, Any]) -> None:
        """[TEMP-EXTERNAL-SKILL] 把 planner 同步后的 env.skill_name 写入 inputs。"""
        skill_name = self._env.skill_name
        if skill_name and "skill_name" not in inputs:
            inputs["skill_name"] = skill_name

    async def run(self, task: str, inputs: dict[str, Any]) -> Any:
        """主入口：规划 -> 执行，失败降级到 DeepAgent（非流式）。"""
        plan_code = await self._planner.plan(task, context=inputs)

        if plan_code is None:
            # 无匹配skill，抛出异常让interface.py处理降级
            raise SkillTurboNotHandled("无匹配skill")

        # [TEMP-EXTERNAL-SKILL] planner 已按路由结果同步 env.skill_name；
        # 写入 inputs 使 HITL resume 持久化的 inputs 携带正确技能名，
        # resume 重放时 executor 的 "not in merged" 保护可还原正确值。
        self._sync_skill_name_to_inputs(inputs)
        try:
            return await self._executor.execute_plan(plan_code, inputs)
        except AbortError:
            # HITL 中断：不能降级，必须向上抛给 adapter 转 HITL 三件套
            raise
        except (PlanCodeValidationError, PlanCodeLoadError) as e:
            # 规划代码校验或加载失败，抛出异常让interface.py处理降级
            raise SkillTurboNotHandled(f"规划代码校验或加载失败: {e}") from e
        except Exception as e:
            # 其他异常，抛出异常让interface.py处理降级
            raise SkillTurboNotHandled(f"规划执行失败: {e}") from e

    async def run_stream(
        self,
        task: str,
        inputs: dict[str, Any],
        request_id: str,
        channel_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """
        主入口：规划 -> 执行，失败降级到 DeepAgent（流式）。

        Args:
            task: 任务描述
            inputs: 输入参数字典
            request_id: 请求ID（用于AgentResponseChunk）
            channel_id: 渠道ID（用于AgentResponseChunk）

        Yields:
            AgentResponseChunk: 流式响应片段（与 DeepAgent 兼容）
        """
        plan_code = await self._planner.plan(task, context=inputs)

        if plan_code is None:
            # 无匹配skill，抛出异常让interface.py处理降级
            raise SkillTurboNotHandled("无匹配skill")

        # [TEMP-EXTERNAL-SKILL] 同 run()：写入正确技能名供 HITL resume 还原。
        self._sync_skill_name_to_inputs(inputs)
        try:
            # 执行规划代码（流式）
            async for chunk in self._executor.execute_plan_stream(
                plan_code, inputs, request_id, channel_id
            ):
                yield chunk
        except AbortError:
            # HITL 中断不降级，透传给 adapter 发 HITL 三件套
            raise
        except (PlanCodeValidationError, PlanCodeLoadError) as e:
            # 规划代码校验或加载失败，抛出异常让interface.py处理降级
            raise SkillTurboNotHandled(f"规划代码校验或加载失败: {e}") from e
        except Exception as e:
            # 其他异常，抛出异常让interface.py处理降级
            raise SkillTurboNotHandled(f"规划执行失败: {e}") from e

    # ──────────────────────── Resume 入口 ────────────────────────

    async def resume_stream(
        self,
        *,
        plan_code: str,
        inputs: dict[str, Any],
        request_id: str,
        channel_id: str,
        pending_tool_call_id: str,
        user_input: Any,
        task_states: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[AgentResponseChunk]:
        """从 HITL 中断点恢复执行：跳过 planner，直接重放 plan_code。

        adapter 在收到带 answers 的请求时调用本方法，提供：
        - 中断时保存的 ``plan_code`` / ``inputs``
        - 中断时记录的 ``pending_tool_call_id``
        - 用户审批回复（``ConfirmPayload`` / dict / etc.）
        - 可选 ``task_states``：中断时任务快照，resume 复用同一套 ``task_id``

        二次执行时 ``SkillTurboExecutor`` 从根节点重放；``task_states`` 中已
        ``completed`` 的二层 Stage 短路跳过真实执行（bash/LLM），仅从
        ``in_progress`` 节点起完整重入。重放到同一 tool_call_id 时将
        ``user_input`` 注入 ctx.extra[RESUME_USER_INPUT_KEY]，
        ``PermissionInterruptRail`` 据此给出 approve / reject。
        """
        self._executor.set_pending_resume(
            expected_tool_call_id=pending_tool_call_id,
            user_input=user_input,
            task_states=task_states,
        )
        try:
            async for chunk in self._executor.execute_plan_stream(
                plan_code, inputs, request_id, channel_id
            ):
                yield chunk
        except AbortError:
            raise
        except (PlanCodeValidationError, PlanCodeLoadError) as e:
            raise SkillTurboNotHandled(f"规划代码校验或加载失败: {e}") from e
        except Exception as e:
            raise SkillTurboNotHandled(f"规划执行失败: {e}") from e
