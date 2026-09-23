# -*- coding: utf-8 -*-
"""_persist_node_artifacts 产物溯源技能名取值（含 HITL resume 场景）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor


def _make_executor(execution_inputs: dict, env_skill_name: str) -> SkillTurboExecutor:
    """绕过 __init__ 的重装配，只填 _persist_node_artifacts 依赖的状态。"""
    executor = SkillTurboExecutor.__new__(SkillTurboExecutor)
    executor._execution_inputs = execution_inputs
    executor._env = SimpleNamespace(skill_name=env_skill_name)
    executor._node_artifacts_holder = {
        "p0_pipeline_init": {"status": "completed"},
    }
    # P2 落盘带 plan_code_hash（resume 重放继承比对），__new__ 绕过 __init__
    # 的 fixture 需显式提供默认空值。
    executor._current_plan_code = ""
    return executor


@pytest.mark.asyncio
async def test_persist_uses_execution_inputs_skill_name_on_resume() -> None:
    """HITL resume 重放：env 未经过 set_skill_name（默认 pptx-craft），
    溯源技能名必须取 merge 后执行 inputs 里持久化的路由结果。"""
    executor = _make_executor(
        execution_inputs={"skill_name": "pptx-content-summarizer"},
        env_skill_name="pptx-craft",
    )
    with patch(
        "jiuwenswarm.server.runtime.skill_turbo.executor.save_node_artifacts",
        new_callable=AsyncMock,
    ) as save_mock:
        await executor._persist_node_artifacts(session=None)
    assert save_mock.await_count == 1
    assert save_mock.call_args.kwargs["skill"] == "pptx-content-summarizer"


@pytest.mark.asyncio
async def test_persist_falls_back_to_env_skill_name() -> None:
    """首次执行（inputs 无 skill_name）：回退 env 构造期/路由同步值。"""
    executor = _make_executor(
        execution_inputs={},
        env_skill_name="pptx-craft",
    )
    with patch(
        "jiuwenswarm.server.runtime.skill_turbo.executor.save_node_artifacts",
        new_callable=AsyncMock,
    ) as save_mock:
        await executor._persist_node_artifacts(session=None)
    assert save_mock.call_args.kwargs["skill"] == "pptx-craft"


@pytest.mark.asyncio
async def test_persist_ignores_empty_skill_name_in_inputs() -> None:
    """inputs 里 skill_name 为空串时视为缺失，回退 env 值。"""
    executor = _make_executor(
        execution_inputs={"skill_name": ""},
        env_skill_name="pptx-craft",
    )
    with patch(
        "jiuwenswarm.server.runtime.skill_turbo.executor.save_node_artifacts",
        new_callable=AsyncMock,
    ) as save_mock:
        await executor._persist_node_artifacts(session=None)
    assert save_mock.call_args.kwargs["skill"] == "pptx-craft"
