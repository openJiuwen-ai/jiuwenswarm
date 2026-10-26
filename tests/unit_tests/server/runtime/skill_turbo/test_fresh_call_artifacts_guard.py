# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fresh 调用守卫：中断残留 node_artifacts 机制性拦截（fresh_start 显式放行）。

复现原故障（先红后绿）：用户停止 turbo PPT 生成后说"继续"，外层 LLM 盲目
fresh 重调 skill_acceleration_exec，新 executor 清盘 node_artifacts 从
Stage 1 全量重跑，已完成工作丢失。守卫返回失败 + 产物摘要引导 LLM 基于产物
文件继续；仅显式携带 fresh_start=true（明确全新任务）才清空记录放行。

修订背景（F_01）：早期"一次性拦截"（拦一次即清库）被外层派发的子代理隔层
重试击穿——子代理看不到外层拒绝 tool_result，重调即放行导致清盘重跑。故拦截
改为机制性：产物记录存在期间持续拦截（拦截不清库），fresh_start=true 显式放行。

修订背景（B_04）：多交付物清单流程（F_03 指南引导 todo_create 建清单）中，
第一个交付物 turbo 成功后产物记录按 F_01 语义保留，第二个交付物的 turbo 调用
（清单推进，早前项已 completed）被本守卫误拦——线上表现为"第二个 PPT 降级
pptx-craft 标准流程"。新增 outer_todo_completed_before 通道（第 9 个
ContextVar 通道，rail 绑定点计算）：早于绑定项存在已完成项 = 后续交付物，
放行（产物清理交 executor 开跑时 _clear_stale_node_artifacts）；无绑定（None）
或绑定项之前无已完成项（0）仍拦——F_01 对"同任务盲目重跑"的保护不放松。
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    reset_skill_turbo_outer_todo_completed_before,
    reset_subsequent_deliverable_registry,
    set_skill_turbo_outer_todo_completed_before,
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


@pytest.fixture(autouse=True)
def _reset_subsequent_deliverable_registry():
    """B_05 放行登记是模块级跨请求单例，测试间必须隔离（键也含固定 _SID）。"""
    reset_subsequent_deliverable_registry()
    yield
    reset_subsequent_deliverable_registry()


@contextlib.contextmanager
def _tool_env(
    adapter: MagicMock,
    turbo,
    guard_session,
    load_artifacts,
    clear_artifacts=None,
    completed_before: int | None = None,
    current_task_id: str | None = None,
):
    """工具调用的环境桩：ContextVar 入口 + 守卫 session + 产物库读写。

    completed_before：B_04 外层 todo 通道桩——None 模拟无外层 todo 绑定
    （默认），int 模拟 rail 绑定分支导出的"早于绑定项的已完成项数"
    （0=绑定项之前无已完成项，≥1=清单推进到后续交付物）。
    current_task_id：B_05 绑定项 id 桩——None 模拟无绑定（默认），str 模拟
    get_current_task_id() 返回的当前绑定外层 todo id（如 "todo:ppt2"）。
    """
    if clear_artifacts is None:
        clear_artifacts = AsyncMock()
    # 经真实 ContextVar 通道注入（而非 patch getter）：同时覆盖
    # set/get/reset 访问器本身，与 context_binding 绑定路径同构。
    token = set_skill_turbo_outer_todo_completed_before(completed_before)
    try:
        with (
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
                "get_current_skill_turbo_adapter",
                return_value=adapter,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
                return_value=current_task_id,
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
    finally:
        reset_skill_turbo_outer_todo_completed_before(token)


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


@pytest.mark.asyncio
async def test_subsequent_deliverable_bypasses_guard_via_outer_todo() -> None:
    """B_04 复现（先红后绿）：多交付物清单第二项的 turbo 调用不再被守卫误拦。

    场景 = 2026-10-21 14:23 线上事件：todo 清单 [PPT1 completed, PPT2
    in_progress]，PPT1 turbo 成功后产物记录按 F_01 语义保留，PPT2 的
    fresh 调用（早于绑定项有 1 个已完成项）此前被守卫拦 → 降级标准流程。
    修复后：completed_before=1 → 放行进 run_stream，且不在守卫内清库
    （产物清理交 executor 开跑时 _clear_stale_node_artifacts，失败兜底同序）。
    """
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(
        adapter, turbo, guard_session, load_artifacts, clear_artifacts,
        completed_before=1,
    ):
        result = await skill_turbo.invoke({"query": "生成杜甫的PPT"})

    assert result.get("success") is True
    turbo.run_stream.assert_called_once()
    # 放行不是清库：守卫不消耗产物记录（executor stale-clear 才是清理方）
    clear_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_deliverable_completed_before_zero_still_blocked() -> None:
    """绑定项之前无已完成项（首个交付物 / 同任务重跑）：守卫照拦不放松。

    F_01 保护面：todo 清单首项 in_progress 时中断残留的产物属于本任务，
    fresh 重跑会清盘丢失——completed_before=0 必须维持机制性拦截。
    """
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(
        adapter, turbo, guard_session, load_artifacts, clear_artifacts,
        completed_before=0,
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    assert "fresh_start=true" in str(result.get("error") or "")
    turbo.run_stream.assert_not_called()
    clear_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_before_none_still_blocked() -> None:
    """无外层 todo 绑定（completed_before=None）：守卫原语义完整保留。

    14:23 事件当时的部署形态（无 F_03 指南、模型未建清单）即此形态——
    B_04 只对"清单推进的后续交付物"放行，盲目 fresh 盲调仍拦。
    """
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(
        adapter, turbo, guard_session, load_artifacts, clear_artifacts,
        completed_before=None,
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    assert "fresh_start=true" in str(result.get("error") or "")
    turbo.run_stream.assert_not_called()
    clear_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_subsequent_deliverable_same_item_rerun_still_blocked() -> None:
    """B_05 收紧：completed_before>0 但绑定项未切换（同项盲目重跑）→ 仍拦。

    场景：todo=[PPT1 completed, PPT2 in_progress]，PPT2 首次调用放行（绑定项
    从无切换到 PPT2）→ PPT2 turbo 成功落盘产物 → 同绑定项（未 todo_modify）
    再次 fresh 调用 = 同项盲目重跑，守卫落回产物检查命中 PPT2 残留 → 拦并
    提示 fresh_start=true，F_01 保护面对第 2+ 项不放松。
    """
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(
        adapter, turbo, guard_session, load_artifacts, clear_artifacts,
        completed_before=1, current_task_id="todo:ppt2",
    ):
        first = await skill_turbo.invoke({"query": "生成杜甫的PPT"})
        second = await skill_turbo.invoke({"query": "生成杜甫的PPT"})

    # 首次：绑定项切换（登记为空 → "todo:ppt2"）→ 放行
    assert first.get("success") is True
    # 第二次：同绑定项（未切换）→ 落回产物检查 → 拦
    assert second.get("success") is False
    assert "fresh_start=true" in str(second.get("error") or "")
    # 首次放行进 run_stream 一次，第二次被拦不进第二次 run_stream
    turbo.run_stream.assert_called_once()
    # 拦截 / 放行均不清库（产物记录是后续拦截的数据源）
    clear_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_subsequent_deliverable_bound_item_switch_releases() -> None:
    """B_05 收紧不误伤清单推进：绑定项切换（PPT2 → PPT3）→ 每次都放行。

    登记只拦"同绑定项重跑"，不拦"清单推进到下一交付物"——确保 B_04 修好的
    "第二个 PPT 不再降级"语义在后续项仍成立。
    """
    adapter = _make_adapter()
    turbo = _fake_turbo()
    guard_session = _make_guard_session()
    load_artifacts = AsyncMock(return_value=dict(_ARTIFACTS_STATE))
    clear_artifacts = AsyncMock()

    with _tool_env(
        adapter, turbo, guard_session, load_artifacts, clear_artifacts,
        completed_before=1, current_task_id="todo:ppt2",
    ):
        first = await skill_turbo.invoke({"query": "生成杜甫的PPT"})

    # 第二项切换绑定（todo_modify 推进到 PPT3）→ completed_before=2
    with _tool_env(
        adapter, turbo, guard_session, load_artifacts, clear_artifacts,
        completed_before=2, current_task_id="todo:ppt3",
    ):
        second = await skill_turbo.invoke({"query": "生成李白的PPT"})

    assert first.get("success") is True
    assert second.get("success") is True
    # 两次均放行进 run_stream（不同绑定项）
    assert turbo.run_stream.call_count == 2
    clear_artifacts.assert_not_awaited()
