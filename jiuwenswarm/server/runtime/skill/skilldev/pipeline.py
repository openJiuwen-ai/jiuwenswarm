# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevPipeline — 确定性状态机编排器.

Pipeline 是整个 SkillDev 流程的骨架：
- 维护阶段跳转顺序（STAGE_HANDLERS 注册表）
- 在挂起点（PLAN_CONFIRM / REVIEW）checkpoint 并暂停
- 提供 run() 和 resume() 两个执行入口
- 每次请求创建、执行到挂起点/完成后释放（不长驻内存）

Pipeline 不关心"怎么做"，只关心"做什么顺序"。
具体逻辑全部委托给各阶段的 StageHandler.execute()。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.deps import SkillDevDeps
from jiuwenswarm.server.runtime.skill.skilldev.schema import (
    SUSPENSION_POINTS,
    SkillDevEvent,
    SkillDevEventType,
    SkillDevStage,
    SkillDevState,
    compute_todos,
)
from jiuwenswarm.server.runtime.skill.skilldev.stages import (
    DescOptimizeStageHandler,
    EvaluateStageHandler,
    GenerateStageHandler,
    ImproveStageHandler,
    InitStageHandler,
    PackageStageHandler,
    PlanStageHandler,
    TestDesignStageHandler,
    TestRunStageHandler,
    ValidateStageHandler,
)

logger = logging.getLogger(__name__)


class SkillDevPipeline:
    """SkillDev 确定性状态机.

    生命周期：每次请求创建 → run()/resume() 执行 → checkpoint → 对象释放。
    不长驻内存，不持有 JiuWenSwarm 实例。
    """

    # PLAN_CONFIRM / REVIEW / DESC_OPTIMIZE_CONFIRM 是挂起点，由 SUSPENSION_POINTS 处理
    STAGE_HANDLERS = {
        SkillDevStage.INIT: InitStageHandler,
        SkillDevStage.PLAN: PlanStageHandler,
        SkillDevStage.GENERATE: GenerateStageHandler,
        SkillDevStage.VALIDATE: ValidateStageHandler,
        SkillDevStage.TEST_DESIGN: TestDesignStageHandler,
        SkillDevStage.TEST_RUN: TestRunStageHandler,
        SkillDevStage.EVALUATE: EvaluateStageHandler,
        SkillDevStage.IMPROVE: ImproveStageHandler,
        SkillDevStage.PACKAGE: PackageStageHandler,
        SkillDevStage.DESC_OPTIMIZE: DescOptimizeStageHandler,
    }

    def __init__(self, task_id: str, state: SkillDevState, deps: SkillDevDeps) -> None:
        self.task_id = task_id
        self.state = state
        self._deps = deps
        self._event_queue: asyncio.Queue = asyncio.Queue()

    async def run(self) -> AsyncIterator[SkillDevEvent]:
        """从当前阶段开始执行，直到遇到挂起点或终态.

        Yields:
            SkillDevEvent：各阶段产生的事件，由 Service 转换为 AgentResponseChunk
        """
        while self.state.stage not in (SkillDevStage.COMPLETED, SkillDevStage.ERROR):
            # 命中挂起点：推送确认请求 → checkpoint → 暂停
            if self.state.stage in SUSPENSION_POINTS:
                suspension = SUSPENSION_POINTS[self.state.stage]
                await self._emit(
                    SkillDevEventType.TODOS_UPDATE,
                    {
                        "todos": compute_todos(self.state.stage, self.state.mode),
                    },
                )
                await self._emit(
                    SkillDevEventType.CONFIRM_REQUEST,
                    {
                        "confirm_type": suspension.confirm_type,
                        "title": suspension.title,
                        "message": suspension.message,
                        "data": suspension.extract_data(self.state),
                        "actions": suspension.actions,
                    },
                )
                await self._checkpoint()
                break

            # 执行当前阶段
            handler_cls = self.STAGE_HANDLERS.get(self.state.stage)
            if handler_cls is None:
                raise RuntimeError(f"阶段 {self.state.stage} 没有对应的处理器")

            workspace = await self._deps.workspace_provider.ensure_local(self.task_id)
            ctx = SkillDevContext(
                task_id=self.task_id,
                deps=self._deps,
                state=self.state,
                workspace=workspace,
                event_queue=self._event_queue,
            )

            await self._emit(
                SkillDevEventType.STAGE_CHANGED,
                {
                    "stage": self.state.stage.value,
                    "iteration": self.state.iteration,
                },
            )
            await self._emit(
                SkillDevEventType.TODOS_UPDATE,
                {
                    "todos": compute_todos(self.state.stage, self.state.mode),
                },
            )

            try:
                handler = handler_cls()
                result = await handler.execute(ctx)
                self.state.stage = result.next_stage
                await self._checkpoint()
            except Exception as exc:
                logger.exception(
                    "[Pipeline] 阶段 %s 执行失败: %s", self.state.stage.value, exc
                )
                self.state.stage = SkillDevStage.ERROR
                self.state.error = str(exc)
                await self._emit(SkillDevEventType.ERROR, {"message": str(exc)})
                await self._checkpoint()
                break

        # 排空事件队列，yield 给调用方
        while not self._event_queue.empty():
            yield self._event_queue.get_nowait()

    async def resume(self, data: dict) -> AsyncIterator[SkillDevEvent]:
        """从挂起点恢复执行.

        Args:
            data: 外部传入的恢复数据（plan 确认内容 / 评测反馈）

        Yields:
            SkillDevEvent：恢复后各阶段产生的事件
        """
        current_stage = self.state.stage
        if current_stage not in SUSPENSION_POINTS:
            raise ValueError(f"阶段 {current_stage} 不是挂起点，无法调用 resume()")

        suspension = SUSPENSION_POINTS[current_stage]

        # 调用 on_resume 更新状态（写入用户确认的 plan / 反馈）
        suspension.on_resume(self.state, data)

        # 计算下一阶段（REVIEW 阶段的 next_stage 是函数，根据 action 动态决定）
        next_stage = suspension.next_stage
        if callable(next_stage):
            next_stage = next_stage(data)
        self.state.stage = next_stage

        async for event in self.run():
            yield event

    async def _emit(self, event_type: SkillDevEventType, payload: dict) -> None:
        """向事件队列写入一个事件."""
        event = SkillDevEvent(
            event_type=event_type,
            payload={"task_id": self.task_id, **payload},
            task_id=self.task_id,
        )
        await self._event_queue.put(event)

    async def _checkpoint(self) -> None:
        """阶段边界：持久化状态 + 同步工作区文件."""
        await self._deps.state_store.save_state(self.task_id, self.state)
        await self._deps.workspace_provider.sync_to_remote(self.task_id)
        logger.debug(
            "[Pipeline] checkpoint: task_id=%s stage=%s",
            self.task_id,
            self.state.stage.value,
        )
