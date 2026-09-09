# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Contracts for the skill_acceleration_exec tool wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    _prepare_parent_stream_output,
    _push_task_event_out_of_band,
    _without_inner_task_routing,
    reset_skill_turbo_outer_todo_active,
    set_skill_turbo_outer_todo_active,
    skill_turbo,
)

_QUERY = "做一份关于黄仁勋GTC讲话的PPT"


async def _empty_stream(*_args, **_kwargs):
    if False:
        yield None


def _fake_turbo():
    inst = MagicMock()
    inst.artifact_holder = {}
    inst.resume_stream = MagicMock(side_effect=_empty_stream)
    inst.run_stream = MagicMock(side_effect=_empty_stream)
    return inst


@pytest.fixture
def _skill_turbo_runtime():
    adapter = MagicMock()
    adapter.build_skill_turbo_config.return_value = MagicMock()
    adapter._instance = MagicMock(card=MagicMock())
    turbo_session = MagicMock()
    turbo_session.post_run = AsyncMock()
    return adapter, turbo_session


def test_skill_acceleration_exec_defers_timeout_to_pipeline() -> None:
    assert skill_turbo.card.name == "skill_acceleration_exec"
    assert skill_turbo.card.properties["resilience"]["timeout_s"] is None


def test_without_inner_task_routing_preserves_stage_content() -> None:
    payload = {
        "event_type": "chat.delta",
        "content": "开始执行 Stage 2: 意图分类（2/14）",
        "task_id": "task_6d61d336",
    }

    cleaned = _without_inner_task_routing(payload)

    assert "task_id" not in cleaned
    assert cleaned["content"] == payload["content"]
    assert payload["task_id"] == "task_6d61d336"


def test_bubble_progress_uses_immediate_parent_content_chunk() -> None:
    payload = {
        "event_type": "chat.delta",
        "content": "开始执行 Stage 8: 深度研究（8/14）",
        "task_id": "task_research",
        "_bubble_progress": True,
    }

    output_type, forwarded = _prepare_parent_stream_output("chat.delta", payload)

    assert output_type == "content_chunk"
    assert forwarded == {
        "event_type": "chat.delta",
        "content": "开始执行 Stage 8: 深度研究（8/14）",
    }
    assert payload["_bubble_progress"] is True
    assert payload["task_id"] == "task_research"


def test_regular_delta_keeps_parent_llm_output_path() -> None:
    payload = {"event_type": "chat.delta", "content": "正在准备图片..."}

    output_type, forwarded = _prepare_parent_stream_output("chat.delta", payload)

    assert output_type == "llm_output"
    assert forwarded is payload


@pytest.mark.asyncio
async def test_load_resume_ctx_failure_still_post_runs_turbo_session(_skill_turbo_runtime) -> None:
    adapter, turbo_session = _skill_turbo_runtime
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value={},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=_fake_turbo(),
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(side_effect=RuntimeError("checkpointer down")),
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    turbo_session.post_run.assert_awaited_once()
    assert result.get("success") is True


@pytest.mark.asyncio
async def test_clear_resume_ctx_failure_does_not_mask_resume_success(_skill_turbo_runtime) -> None:
    adapter, turbo_session = _skill_turbo_runtime
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value={},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=_fake_turbo(),
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_ctx",
            AsyncMock(side_effect=RuntimeError("clear failed")),
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    turbo_session.post_run.assert_awaited_once()
    assert result.get("success") is True
    assert "执行失败" not in str(result.get("error") or "")


def _raise_on_resume(exc: BaseException):
    async def _stream(*_args, **_kwargs):
        if False:
            yield None
        raise exc

    inst = MagicMock()
    inst.artifact_holder = {}
    inst.resume_stream = MagicMock(side_effect=_stream)
    inst.run_stream = MagicMock(side_effect=_empty_stream)
    return inst


_RECOVERY_HINT_KEY = "skill_turbo_interrupt_recovery"
_RECOVERY_SUMMARY = (
    "[SkillAccelerationExec (ppt) 已完成节点产物]\n"
    "- p1_outline: status=completed | 文件: /tmp/out/outline.md"
)


def _recovery_metadata(consumed: bool = False) -> dict:
    """构造带一次性中断恢复 hint 的 request metadata（模拟 prepare 注入后的形态）。"""
    return {
        "request_id": "req-recovery",
        "channel_id": "web",
        "session_id": "sess-recovery",
        _RECOVERY_HINT_KEY: {
            "summary": _RECOVERY_SUMMARY,
            "skill": "ppt",
            "request_id": "req-recovery",
            "consumed": consumed,
        },
    }


@pytest.mark.asyncio
async def test_interrupt_recovery_guard_rejects_fresh_call_once(
    _skill_turbo_runtime,
) -> None:
    """有未消费中断产物的请求内，fresh 调用必须被一次性拒绝而非从 p0 重跑。

    复现 #3755 问题 1：中断任务有 14 个节点产物，下一条消息被 LLM 以全新
    skill_acceleration_exec 调用重启 → executor 清盘 → 从 p0 全量重跑。
    守卫应拒绝并附产物摘要，引导 LLM 走非 skillTurbo 流程继续。
    """
    adapter, _turbo_session = _skill_turbo_runtime
    turbo = _fake_turbo()
    metadata = _recovery_metadata()
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value=metadata,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=turbo,
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    error = str(result.get("error") or "")
    assert _RECOVERY_SUMMARY in error
    assert "skill_tool" in error
    # 拒绝时绝不能启动新 executor（那是从 p0 清盘重跑的入口）
    turbo.run_stream.assert_not_called()
    # hint 被消费：同请求内重试将放行
    assert metadata[_RECOVERY_HINT_KEY]["consumed"] is True


@pytest.mark.asyncio
async def test_interrupt_recovery_guard_passes_retry_after_consumption(
    _skill_turbo_runtime,
) -> None:
    """LLM 明确重试（视为全新任务）时守卫放行，正常走 run_stream。"""
    adapter, _turbo_session = _skill_turbo_runtime
    turbo = _fake_turbo()
    metadata = _recovery_metadata(consumed=True)
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value=metadata,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=turbo,
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    turbo.run_stream.assert_called_once()
    assert result.get("success") is True


@pytest.mark.asyncio
async def test_interrupt_recovery_guard_does_not_block_hitl_resume(
    _skill_turbo_runtime,
) -> None:
    """合法 HITL resume（结构化作答重放）不受守卫影响：必须仍走 resume_stream。"""
    adapter, turbo_session = _skill_turbo_runtime
    turbo = _fake_turbo()
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value=_recovery_metadata(),
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=turbo,
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    turbo.resume_stream.assert_called_once()
    turbo.run_stream.assert_not_called()
    assert result.get("success") is True



@pytest.mark.asyncio
async def test_hitl_abort_does_not_clear_or_post_run_turbo_session(
    _skill_turbo_runtime,
) -> None:
    adapter, turbo_session = _skill_turbo_runtime
    turbo_session.close_stream = AsyncMock()
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    tic = SimpleNamespace(tool_call=SimpleNamespace(id="tc-ask"))
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value={},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=_raise_on_resume(AbortError("ask_user")),
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_ctx",
            AsyncMock(),
        ) as clear_ctx,
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.extract_tool_interrupt",
            return_value=tic,
        ),
    ):
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            _SKILL_TURBO_HITL_PLACEHOLDER,
            set_skill_turbo_hitl_tic,
        )

        set_skill_turbo_hitl_tic(None)
        try:
            result = await skill_turbo.invoke({"query": _QUERY})
        finally:
            set_skill_turbo_hitl_tic(None)

    assert result == _SKILL_TURBO_HITL_PLACEHOLDER
    clear_ctx.assert_not_awaited()
    turbo_session.post_run.assert_not_awaited()
    turbo_session.close_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_abort_without_tic_keeps_resume_ctx(_skill_turbo_runtime) -> None:
    adapter, turbo_session = _skill_turbo_runtime
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value={},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=_raise_on_resume(AbortError("paused")),
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_ctx",
            AsyncMock(),
        ) as clear_ctx,
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.extract_tool_interrupt",
            return_value=None,
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    clear_ctx.assert_not_awaited()
    turbo_session.post_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_exception_keeps_resume_ctx(_skill_turbo_runtime) -> None:
    adapter, turbo_session = _skill_turbo_runtime
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value={},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=_raise_on_resume(RuntimeError("boom")),
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_ctx",
            AsyncMock(),
        ) as clear_ctx,
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    clear_ctx.assert_not_awaited()
    turbo_session.post_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_handled_clears_resume_ctx(_skill_turbo_runtime) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.agent import SkillTurboNotHandled

    adapter, turbo_session = _skill_turbo_runtime
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    with (
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
            return_value=adapter,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
            return_value={"audience": "高管"},
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
            return_value={},
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
            return_value=None,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=_raise_on_resume(SkillTurboNotHandled("no skill")),
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            return_value=turbo_session,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_ctx",
            AsyncMock(),
        ) as clear_ctx,
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    clear_ctx.assert_awaited_once_with(turbo_session)
    turbo_session.post_run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outer_todo_active", "expected_event_types"),
    [
        (True, ["chat.delta", "chat.usage_metadata"]),
        (False, ["task.update", "chat.delta", "chat.usage_metadata"]),
    ],
)
async def test_outer_todo_hides_inner_tasks_without_hiding_stage_messages(
    _skill_turbo_runtime,
    outer_todo_active: bool,
    expected_event_types: list[str],
) -> None:
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-1",
    )

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(
            payload={
                "event_type": "task.update",
                "tasks": [{
                    "task_id": "task_deadbeef",
                    "task_content": "Stage 1: 流水线初始化",
                    "status": "in_progress",
                }],
            }
        )
        yield SimpleNamespace(
            payload={
                "event_type": "chat.delta",
                "content": "开始执行 Stage 1: 流水线初始化（1/14）\n",
                "task_id": "task_deadbeef",
            }
        )
        yield SimpleNamespace(
            payload={
                "event_type": "chat.usage_metadata",
                "metadata": {"plan_name": "p1_intent_classify"},
                "task_id": "task_6d61d336",
            }
        )

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(outer_todo_active)
    try:
        with (
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
                return_value=adapter,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
                return_value=None,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
                return_value=None,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
                return_value={},
            ),
            patch(
                "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
                return_value=parent_session,
            ),
            patch(
                "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
                return_value=None,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
                return_value=turbo,
            ),
        ):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    assert result.get("success") is True
    forwarded = [
        call.args[0].payload
        for call in parent_session.write_stream.await_args_list
    ]
    assert [
        payload["event_type"] for payload in forwarded
    ] == expected_event_types
    delta = next(
        payload for payload in forwarded
        if payload["event_type"] == "chat.delta"
    )
    assert "Stage 1: 流水线初始化" in delta["content"]
    if outer_todo_active:
        assert all("task_id" not in payload for payload in forwarded)
    else:
        assert forwarded[0]["tasks"][0]["task_id"] == "task_deadbeef"


@pytest.mark.asyncio
async def test_task_update_is_also_pushed_out_of_band(_skill_turbo_runtime) -> None:
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-fallback",
    )
    send_push = AsyncMock(return_value=1)
    server = SimpleNamespace(send_push=send_push)

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(
            payload={
                "event_type": "task.start",
                "task_id": "task_a2f23a2e",
                "task_content": "Stage 11: 幻灯片生成",
            }
        )
        yield SimpleNamespace(
            payload={
                "event_type": "task.update",
                "tasks": [{
                    "task_id": "task_a2f23a2e",
                    "task_content": "Stage 11: 幻灯片生成",
                    "status": "completed",
                }],
            }
        )
        yield SimpleNamespace(
            payload={
                "event_type": "task.complete",
                "task_id": "task_a2f23a2e",
                "task_content": "Stage 11: 幻灯片生成",
                "status": "completed",
            }
        )
        yield SimpleNamespace(
            payload={
                "event_type": "chat.delta",
                "content": "页面生成完成",
            }
        )

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(False)
    try:
        with (
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_skill_turbo_adapter",
                return_value=adapter,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_task_id",
                return_value=None,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_skill_turbo_resume_answers",
                return_value=None,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools.get_current_request_metadata",
                return_value={
                    "request_id": "7fcaa6ad-4b55-454e-909e-8a2897be8d05",
                    "channel_id": "officeclaw",
                    "session_id": "officeclaw_e0495cc952026dae2fb252ef",
                },
            ),
            patch(
                "jiuwenswarm.agents.harness.common.tools.subagent_executor.get_subagent_parent_session",
                return_value=parent_session,
            ),
            patch(
                "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_effective_request_workspace_dir",
                return_value=None,
            ),
            patch(
                "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
                return_value=turbo,
            ),
            patch(
                "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
                return_value=server,
            ),
        ):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    assert result.get("success") is True
    send_push.assert_awaited_once()
    pushed = send_push.await_args.args[0]
    assert pushed["request_id"] == "7fcaa6ad-4b55-454e-909e-8a2897be8d05"
    assert pushed["session_id"] == "officeclaw_e0495cc952026dae2fb252ef"
    assert pushed["payload"]["event_type"] == "task.update"
    assert pushed["payload"]["tasks"][0]["task_content"] == "Stage 11: 幻灯片生成"
    assert parent_session.write_stream.await_count == 4


@pytest.mark.asyncio
async def test_push_task_event_skips_non_task_events() -> None:
    send_push = AsyncMock(return_value=1)
    server = SimpleNamespace(send_push=send_push)
    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=server,
    ):
        await _push_task_event_out_of_band(
            "chat.delta",
            {"event_type": "chat.delta", "content": "x"},
            request_id="r1",
            channel_id="officeclaw",
            session_id="sess-1",
        )
    send_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_task_event_skips_start_and_complete() -> None:
    send_push = AsyncMock(return_value=1)
    server = SimpleNamespace(send_push=send_push)
    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=server,
    ):
        await _push_task_event_out_of_band(
            "task.start",
            {"event_type": "task.start", "task_id": "task_a"},
            request_id="r1",
            channel_id="officeclaw",
            session_id="sess-1",
        )
        await _push_task_event_out_of_band(
            "task.complete",
            {"event_type": "task.complete", "task_id": "task_a"},
            request_id="r1",
            channel_id="officeclaw",
            session_id="sess-1",
        )
    send_push.assert_not_awaited()
