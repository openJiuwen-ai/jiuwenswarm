# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevPipeline — 确定性状态机编排器.

Pipeline 是整个 SkillDev 流程的骨架：
- 维护阶段跳转顺序（STAGE_HANDLERS 注册表）
- 在挂起点（QUESTION_CLARIFY / SKIP_TESTS_CONFIRM / REVIEW / DESC_OPTIMIZE_CONFIRM）checkpoint 并暂停
- 提供 run() 和 resume() 两个执行入口
- 每次请求创建、执行到挂起点/完成后释放（不长驻内存）

Pipeline 不关心"怎么做"，只关心"做什么顺序"。
具体逻辑全部委托给各阶段的 StageHandler.execute()。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.schema import (
    SUSPENSION_POINTS,
    SkillDevEvent,
    SkillDevEventType,
    SkillDevStage,
    SkillDevState,
    compute_todos,
)
from jiuwenclaw.agentserver.skilldev.stages import (
    ClarifyStageHandler,
    DescOptimizeStageHandler,
    EvaluateStageHandler,
    GenerateStageHandler,
    ImproveStageHandler,
    InitStageHandler,
    PackageStageHandler,
    TestDesignStageHandler,
    TestRunStageHandler,
    ValidateStageHandler,
)

logger = logging.getLogger(__name__)


class SkillDevPipeline:
    """SkillDev 确定性状态机.

    生命周期：每次请求创建 → run()/resume() 执行 → checkpoint → 对象释放。
    不长驻内存，不持有 JiuWenClaw 实例。
    """

    # QUESTION_CLARIFY / SKIP_TESTS_CONFIRM / REVIEW / DESC_OPTIMIZE_CONFIRM 是挂起点，由 SUSPENSION_POINTS 处理
    STAGE_HANDLERS = {
        SkillDevStage.INIT: InitStageHandler,
        SkillDevStage.CLARIFY: ClarifyStageHandler,
        SkillDevStage.GENERATE: GenerateStageHandler,
        SkillDevStage.VALIDATE: ValidateStageHandler,
        SkillDevStage.TEST_DESIGN: TestDesignStageHandler,
        SkillDevStage.TEST_RUN: TestRunStageHandler,
        SkillDevStage.EVALUATE: EvaluateStageHandler,
        SkillDevStage.IMPROVE: ImproveStageHandler,
        SkillDevStage.DESC_OPTIMIZE: DescOptimizeStageHandler,
        SkillDevStage.PACKAGE: PackageStageHandler,
    }

    def __init__(self, task_id: str, state: SkillDevState, deps: SkillDevDeps) -> None:
        self.task_id = task_id
        self.state = state
        self._deps = deps
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._last_handler_result = None

    async def run(self) -> AsyncIterator[SkillDevEvent]:
        """从当前阶段开始执行，直到遇到挂起点或终态.

        事件实时流式推送：handler 在后台 Task 中执行，主生成器并发消费
        事件队列并立即 yield，使前端能实时收到每个阶段的中间事件。

        Yields:
            SkillDevEvent：各阶段产生的事件，由 Service 转换为 AgentResponseChunk
        """
        cancel_event = self._deps.cancel_events.get(self.task_id)
        while self.state.stage not in (SkillDevStage.COMPLETED, SkillDevStage.ERROR):
            # 检测外部取消信号（由 skilldev.cancel 请求触发）
            if cancel_event and cancel_event.is_set():
                logger.info("[session=%s] [Pipeline] 收到取消信号，终止任务", self.task_id)
                self.state.stage = SkillDevStage.ERROR
                self.state.error = "任务已取消"
                await self._checkpoint()
                async for evt in self._drain_events():
                    yield evt
                break
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
                async for evt in self._drain_events():
                    yield evt
                break

            # 执行当前阶段
            handler_cls = self.STAGE_HANDLERS.get(self.state.stage)

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
            # 立即推送 STAGE_CHANGED / TODOS_UPDATE，不等 handler 执行完
            async for evt in self._drain_events():
                yield evt

            try:
                handler = handler_cls()
                # handler 放入后台 Task，主循环并发消费队列实时 yield
                async for evt in self._run_handler_streaming(handler, ctx):
                    yield evt
                result = self._last_handler_result
                logger.info(
                    "[session=%s] [Pipeline] 阶段 %s 执行完成，即将进入阶段 %s",
                    self.task_id,
                    self.state.stage.value,
                    result.next_stage,
                )
                self.state.stage = result.next_stage
                await self._checkpoint()

                if self.state.stage == SkillDevStage.COMPLETED:
                    await self._emit(
                        SkillDevEventType.TODOS_UPDATE,
                        {
                            "todos": compute_todos(self.state.stage, self.state.mode),
                        },
                    )

            except Exception as exc:
                logger.exception(
                    "[session=%s] [Pipeline] 阶段 %s 执行失败: %s",
                    self.task_id,
                    self.state.stage.value,
                    exc,
                )
                self.state.stage = SkillDevStage.ERROR
                self.state.error = str(exc)
                await self._checkpoint()
                async for evt in self._drain_events():
                    yield evt
                break

        # 安全兜底：排空残留事件
        async for evt in self._drain_events():
            yield evt

    async def resume(self, data: dict) -> AsyncIterator[SkillDevEvent]:
        """从挂起点恢复执行.

        Args:
            data: 外部传入的恢复数据（plan 确认内容 / 评测反馈）

        Yields:
            SkillDevEvent：恢复后各阶段产生的事件
        """
        current_stage = self.state.stage

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

    async def _run_handler_streaming(
        self, handler, ctx: SkillDevContext,
    ) -> AsyncIterator[SkillDevEvent]:
        """后台执行 handler.execute()，前台并发消费事件队列.

        将 handler 放入 asyncio.Task，主协程在 handler 运行期间不断从
        事件队列取出事件并 yield，实现实时流式推送。handler 完成后将
        StageResult 存入 self._last_handler_result。
        """
        task = asyncio.create_task(handler.execute(ctx))
        try:
            while not task.done():
                try:
                    event = await asyncio.wait_for(
                        self._event_queue.get(), timeout=0.1,
                    )
                    yield event
                except asyncio.TimeoutError:
                    continue
            # handler 已完成，获取结果（若 handler 抛异常则此处重新抛出）
            self._last_handler_result = task.result()
        except BaseException:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        # 排空 handler 完成瞬间的残留事件
        async for evt in self._drain_events():
            yield evt

    async def _drain_events(self) -> AsyncIterator[SkillDevEvent]:
        """非阻塞排空队列中所有已有事件."""
        while not self._event_queue.empty():
            yield self._event_queue.get_nowait()

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
            "[session=%s] [Pipeline] checkpoint: stage=%s",
            self.task_id,
            self.state.stage.value,
        )
