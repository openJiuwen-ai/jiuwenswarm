# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FallbackContractError 降级路径的 stage 任务终态事件转发。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    FallbackContractError,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode


class _ContractFailStageNode(PlanNode):
    """depth=1 stage：子节点契约失败上抛（模拟 p0_1_env_deps 环境定位失败）。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p0_pipeline_init",
            instruction="stage",
            sub_plans=[],
            depth=1,
        )

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}

    async def _execute_stream(self, inputs: dict[str, Any]):
        yield {"node": self.plan_name, "status": "progress", "message": "正在检测环境依赖"}
        raise FallbackContractError(
            node_name="p0_1_env_deps",
            reason="无法定位 pptx-craft 根目录",
        )


class _RootNode(PlanNode):
    """depth=0 根节点：经 execute_subplan_stream 驱动二层 stage（触发任务回调链）。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="ppt_gen_root",
            instruction="root",
            sub_plans=[_ContractFailStageNode()],
            depth=0,
        )

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}

    async def _execute_stream(self, inputs: dict[str, Any]):
        async for chunk in self.execute_subplan_stream(self.sub_plans[0], inputs):
            yield chunk


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = (
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes",
    )
    return SkillTurboExecutor(environment=env)


@pytest.mark.asyncio
async def test_fallback_contract_error_flushes_stage_task_events():
    """验证 FallbackContractError 降级时 in-progress stage 的 task.complete(failed) 与 failed 快照随流发出。"""
    from jiuwenswarm.server.runtime.skill_turbo import executor as executor_mod

    ex = _make_executor()
    root = _RootNode()
    ex._bind_node_callbacks(root)

    queue: list[dict] = []
    task_states: dict[str, dict[str, Any]] = {}
    token_queue = executor_mod._task_events_queue_var.set(queue)
    token_states = executor_mod._task_states_var.set(task_states)
    try:
        chunks = []
        with pytest.raises(FallbackContractError):
            async for chunk in ex._execute_node_stream(
                root, {}, "request-1", "channel-1"
            ):
                chunks.append(chunk)
    finally:
        executor_mod._task_states_var.reset(token_states)
        executor_mod._task_events_queue_var.reset(token_queue)

    payloads = [c.payload for c in chunks if isinstance(c.payload, dict)]

    task_starts = [p for p in payloads if p.get("event_type") == "task.start"]
    assert task_starts, "task.start 未随流发出"

    task_completes = [p for p in payloads if p.get("event_type") == "task.complete"]
    assert task_completes, (
        "stage 失败终态 task.complete 未随流发出，前端任务面板将停滞在 in_progress"
    )
    assert task_completes[-1].get("status") == "failed"

    task_updates = [p for p in payloads if p.get("event_type") == "task.update"]
    has_failed_snapshot = any(
        any(t.get("status") == "failed" for t in p.get("tasks", []))
        for p in task_updates
    )
    assert has_failed_snapshot, "failed 任务快照未随流发出"
    assert queue == [], "任务事件滞留队列未发出"
