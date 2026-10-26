# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""resume task_states 快照恢复与单 running 收口（PR 5036 修复路径，S_01 状态机）。

防护意图：PR 5036 引入 task_states 快照保存/恢复（+220 行）解决"resume 后
前端 taskRuns 叠出两套 Stage"，但该修复零测试。本文件锁定两个纯逻辑核心：
  1. _restore_task_states_from_snapshot：按 display name/index 对齐复用原
     task_id（复用失败=叠卡事故回归）
  2. _park_later_in_progress_tasks：同刻只允许一个 running（多 in_progress
     收回 pending，前端单 running 契约）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from jiuwenswarm.server.runtime.skill_turbo.executor import (
    ExecutorConfig,
    SkillTurboExecutor,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode


class _Stage(PlanNode):
    def __init__(self, name: str) -> None:
        super().__init__(plan_name=name, instruction="stage", sub_plans=[], depth=1)

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}


class _Root(PlanNode):
    def __init__(self, stages: list[PlanNode]) -> None:
        super().__init__(plan_name="ppt_gen_root", instruction="root", sub_plans=stages, depth=0)

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = ("skill_codes",)
    with (
        patch.object(SkillTurboExecutor, "_build_ask_user_rail", return_value=None),
        patch.object(SkillTurboExecutor, "_build_llm_retry_rail", return_value=None),
    ):
        return SkillTurboExecutor(env, config=ExecutorConfig())


def _snapshot(
    task_id: str, content: str, index: int, status: str = "completed"
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_content": content,
        "task_index": index,
        "status": status,
        "source": "skill_turbo",
    }


class TestRestoreTaskStatesFromSnapshot:
    def test_reuses_task_id_by_display_name(self):
        """快照命中 display name → 复用原 task_id（防前端叠出第二套 Stage）。"""
        ex = _make_executor()
        root = _Root([_Stage("p0_pipeline_init"), _Stage("p1_intent_classify")])
        saved = [
            _snapshot("task_A", "p0_pipeline_init", 0, "completed"),
            _snapshot("task_B", "p1_intent_classify", 1, "in_progress"),
        ]

        restored = ex._restore_task_states_from_snapshot(root, saved)

        by_content = {s["task_content"]: s for s in restored.values()}
        assert by_content["p0_pipeline_init"]["task_id"] == "task_A"
        assert by_content["p0_pipeline_init"]["status"] == "completed"
        assert by_content["p1_intent_classify"]["task_id"] == "task_B"
        assert by_content["p1_intent_classify"]["status"] == "in_progress"

    def test_falls_back_to_index_when_content_mismatched(self):
        """display name 对不上（改名/漂移）→ 按 index 对齐，仍复用 task_id。"""
        ex = _make_executor()
        root = _Root([_Stage("renamed_stage")])
        saved = [_snapshot("task_X", "old_stage_name", 0, "completed")]

        restored = ex._restore_task_states_from_snapshot(root, saved)
        (state,) = restored.values()
        assert state["task_id"] == "task_X"
        assert state["task_content"] == "renamed_stage", "task_content 刷新为当前树的名字"
        assert state["task_index"] == 0

    def test_new_stage_without_snapshot_gets_fresh_task_id(self):
        """新增 stage（快照无记录）→ 分配新 task_id、pending 状态。"""
        ex = _make_executor()
        root = _Root([_Stage("p0_pipeline_init"), _Stage("p2_brand_new")])
        saved = [_snapshot("task_A", "p0_pipeline_init", 0)]

        restored = ex._restore_task_states_from_snapshot(root, saved)
        by_content = {s["task_content"]: s for s in restored.values()}
        assert by_content["p2_brand_new"]["status"] == "pending"
        assert by_content["p2_brand_new"]["task_id"] != "task_A"

    def test_duplicate_snapshot_ids_not_reused(self):
        """快照含重复 task_id（历史脏数据）→ 后者不复用，防两 stage 共享同一 id。"""
        ex = _make_executor()
        root = _Root([_Stage("s1"), _Stage("s2")])
        saved = [
            _snapshot("dup_id", "s1", 0),
            _snapshot("dup_id", "s2", 1),
        ]

        restored = ex._restore_task_states_from_snapshot(root, saved)
        ids = {s["task_id"] for s in restored.values()}
        assert len(ids) == 2, "两个 stage 不得共享同一 task_id"


class TestParkLaterInProgressTasks:
    def test_later_in_progress_parked_to_pending(self):
        """index 更大且仍 in_progress 的 stage 收成 pending（单 running 契约）。"""
        ex = _make_executor()
        current = {"task_id": "t0", "task_index": 0, "status": "in_progress"}
        later = {"task_id": "t2", "task_index": 2, "status": "in_progress", "start_time": 1.0}
        earlier_done = {"task_id": "t1", "task_index": 1, "status": "completed"}

        ex._park_later_in_progress_tasks(current, {current["task_id"]: current, "t2": later, "t1": earlier_done})

        assert later["status"] == "pending", "更靠后的 in_progress 必须收回 pending"
        assert "start_time" not in later, "收回时须清 start_time（防下次时长统计污染"
        assert current["status"] == "in_progress", "当前 stage 不受影响"
        assert earlier_done["status"] == "completed", "非 in_progress 状态不动"

    def test_earlier_in_progress_not_parked(self):
        """index 更小的 in_progress 不收回（只往后看）。"""
        ex = _make_executor()
        current = {"task_id": "t2", "task_index": 2, "status": "in_progress"}
        earlier = {"task_id": "t0", "task_index": 0, "status": "in_progress"}

        ex._park_later_in_progress_tasks(current, {"t2": current, "t0": earlier})

        assert earlier["status"] == "in_progress"

    def test_current_without_index_is_noop(self):
        """current 无 task_index（异常形态）→ 不动任何状态（防御分支）。"""
        ex = _make_executor()
        current = {"task_id": "t0", "status": "in_progress"}
        other = {"task_id": "t1", "task_index": 1, "status": "in_progress"}

        ex._park_later_in_progress_tasks(current, {"t0": current, "t1": other})

        assert other["status"] == "in_progress"
