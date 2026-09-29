# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fresh 调用守卫：中断残留 node_artifacts 机制性拦截（fresh_start 显式放行）。

复现原故障（先红后绿）：用户停止 turbo PPT 生成后说"继续"，外层 LLM 盲目
fresh 重调 skill_acceleration_exec，新 executor 清盘 node_artifacts 从
Stage 1 全量重跑，已完成工作丢失。守卫返回失败 + 产物摘要引导 LLM 基于产物
文件继续；仅显式携带 fresh_start=true（明确全新任务）才清空记录放行。

修订背景（F_01）：早期"一次性拦截"（拦一次即清库）被外层派发的子代理隔层
重试击穿——子代理看不到外层拒绝 tool_result，重调即放行导致清盘重跑。故拦截
改为机制性：产物记录存在期间持续拦截（拦截不清库），fresh_start=true 显式放行。
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    skill_turbo,
)

_QUERY = "继续完成那份PPT"
_SID = "sess-guard-1"
_METADATA = {"request_id": "req-1", "channel_id": "ch-1", "session_id": _SID}

_ARTIFACTS_STATE = {
    "skill": "pptx-craft",
    "plan_code_hash": "abc123",
    "nodes": {
        "p1_outline": {
            "info": {"stage": "outline"},
            "files": [{"path": "D:/out/ppt/outline.json"}],
        },
        "p3_layout": {
            "info": {"stage": "layout"},
            "files": [{"path": "D:/out/ppt/layout.json"}],
        },
    },
}


async def _empty_stream(*_args, **_kwargs):
    if False:
        yield None


def _fake_turbo():
    inst = MagicMock()
    inst.artifact_holder = {}
    inst.run_stream = MagicMock(side_effect=_empty_stream)
    inst.resume_stream = MagicMock(side_effect=_empty_stream)
    return inst


def _make_adapter():
    adapter = MagicMock()
    adapter.build_skill_turbo_config.return_value = MagicMock()
    adapter._instance = MagicMock(card=MagicMock())
    return adapter


def _make_guard_session():
    session = MagicMock()
    session.pre_run = AsyncMock()
    session.post_run = AsyncMock()
    return session


@contextlib.contextmanager
def _tool_env(adapter: MagicMock, turbo, guard_session, load_artifacts, clear_artifacts=None):
    """工具调用的环境桩：ContextVar 入口 + 守卫 session + 产物库读写。"""
    if clear_artifacts is None:
        clear_artifacts = AsyncMock()
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_skill_turbo_resume_answers",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_current_request_metadata",
            return_value=dict(_METADATA),
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor."
            "get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor."
            "context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=turbo,
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=guard_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.node_artifact_store."
            "load_node_artifacts",
            load_artifacts,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.node_artifact_store."
            "clear_node_artifacts",
            clear_artifacts,
        ),
    ):
        yield clear_artifacts


@pytest.mark.asyncio
async def test_fresh_call_with_stale_artifacts_rejected_with_summary() -> None:
    """残留产物时 fresh 调用被拒：success=False + 摘要 + 引导，不进 run_stream，不清库。"""
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(adapter, turbo, guard_session, load_artifacts, clear_artifacts):
        result = await skill_turbo.invoke({"query": _QUERY})

    # 拦截：失败 + 引导文案，不构造 turbo 执行
    assert result.get("success") is False
    error = str(result.get("error") or "")
    assert "skill_tool" in error
    assert "skill_acceleration_exec" in error
    assert "fresh_start=true" in error
    # 摘要包含产物条目与文件路径（LLM 基于此继续）
    assert "p1_outline" in error
    assert "D:/out/ppt/outline.json" in error
    assert "pptx-craft" in error
    turbo.run_stream.assert_not_called()
    # 机制性拦截：拦截不清库（产物记录是 hash 继承与后续拦截的数据源）
    clear_artifacts.assert_not_awaited()
    guard_session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeat_fresh_call_still_rejected_until_fresh_start() -> None:
    """机制性拦截核心语义：拒绝后重试（子代理隔层重试场景）仍被拦，产物记录不消耗。"""
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(adapter, turbo, guard_session, load_artifacts, clear_artifacts):
        first = await skill_turbo.invoke({"query": _QUERY})
        second = await skill_turbo.invoke({"query": "resume the paused pptx task"})

    assert first.get("success") is False
    # 未携带 fresh_start 的重复调用（子代理拿不到外层拒绝 tool_result）持续被拦
    assert second.get("success") is False
    assert "fresh_start=true" in str(second.get("error") or "")
    turbo.run_stream.assert_not_called()
    clear_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_start_true_clears_artifacts_and_passes() -> None:
    """fresh_start=true 显式全新任务：清空产物记录后放行进 run_stream。"""
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(adapter, turbo, guard_session, load_artifacts, clear_artifacts):
        result = await skill_turbo.invoke(
            {"query": "做一个完全不同的新主题PPT", "fresh_start": True}
        )

    assert result.get("success") is True
    turbo.run_stream.assert_called_once()
    clear_artifacts.assert_awaited_once()
    guard_session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_start_clear_failure_still_passes() -> None:
    """fresh_start=true 但清库失败：仍放行（executor 的 stale 清盘兜底），不拦截。"""
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock(side_effect=RuntimeError("checkpointer down"))

    with _tool_env(adapter, turbo, guard_session, load_artifacts, clear_artifacts):
        result = await skill_turbo.invoke(
            {"query": "做一个完全不同的新主题PPT", "fresh_start": True}
        )

    assert result.get("success") is True
    turbo.run_stream.assert_called_once()
    clear_artifacts.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_artifacts_guard_noop_and_runs_fresh() -> None:
    """无残留产物（正常首跑）时守卫零影响，直接进 run_stream。"""
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=None)

    with _tool_env(adapter, turbo, guard_session, load_artifacts) as clear_artifacts:
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is True
    turbo.run_stream.assert_called_once()
    clear_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_failure_fails_open() -> None:
    """守卫自身异常（load 抛错）fail-open：fresh 调用照常执行。"""
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(side_effect=RuntimeError("state corrupted"))

    with _tool_env(adapter, turbo, guard_session, load_artifacts):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is True
    turbo.run_stream.assert_called_once()
