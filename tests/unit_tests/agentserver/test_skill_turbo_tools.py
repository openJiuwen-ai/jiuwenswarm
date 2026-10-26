# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Contracts for the skill_acceleration_exec tool wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    SKILL_TURBO_MODEL_IDENTITY_KEY,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    _prepare_parent_stream_output,
    _push_task_event_out_of_band,
    _without_inner_task_routing,
    reset_skill_turbo_outer_todo_active,
    set_skill_turbo_outer_todo_active,
    skill_turbo,
)
from jiuwenswarm.server.runtime.skill_turbo import task_slot_guard

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




def _isolated_session_side_effect(main_session: MagicMock):
    """create_agent_session 的 side_effect：首次调用返回 main_session，之后返回独立
    隔离 session mock（clear_resume_in_flight / clear_resume_ctx 的隔离键 session），
    使隔离 session 的 post_run 不计入 main_session 的断言。
    """
    counter = {"n": 0}

    def _factory(*_args, **_kwargs):
        counter["n"] += 1
        if counter["n"] == 1:
            return main_session
        isolated = MagicMock()
        isolated.pre_run = AsyncMock()
        isolated.post_run = AsyncMock()
        isolated.close_stream = AsyncMock()
        return isolated

    return _factory


def _forwarding_env(
    adapter,
    turbo,
    parent_session,
    *,
    metadata: dict | None = None,
    push_server=None,
):
    """skill_turbo.invoke 事件转发路径的公共 patch 集（B_03 用例组共用）。

    返回 ExitStack（with 使用）；push_server 提供时补 AgentWebSocketServer
    补丁（OOB 断言用）。"""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_current_skill_turbo_adapter",
            return_value=adapter,
        )
    )
    stack.enter_context(
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_current_task_id",
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_skill_turbo_resume_answers",
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools."
            "get_current_request_metadata",
            return_value=metadata or {},
        )
    )
    stack.enter_context(
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor."
            "get_subagent_parent_session",
            return_value=parent_session,
        )
    )
    stack.enter_context(
        patch(
            "jiuwenswarm.agents.harness.common.tools.subagent_executor."
            "context_vars.get_effective_request_workspace_dir",
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
            return_value=turbo,
        )
    )
    if push_server is not None:
        stack.enter_context(
            patch(
                "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer."
                "get_instance",
                return_value=push_server,
            )
        )
    return stack
@pytest.fixture
def _skill_turbo_runtime():
    adapter = MagicMock()
    adapter.build_skill_turbo_config.return_value = MagicMock()
    adapter._instance = MagicMock(card=MagicMock())
    turbo_session = MagicMock()
    turbo_session.post_run = AsyncMock()
    return adapter, turbo_session


@pytest.fixture(autouse=True)
def _reset_task_slot_guard():
    """B_03：槽位登记簿是模块级跨请求状态，用例间必须隔离。"""
    task_slot_guard.reset_task_slot_registry()
    yield
    task_slot_guard.reset_task_slot_registry()


def test_skill_acceleration_exec_defers_timeout_to_pipeline() -> None:
    assert skill_turbo.card.name == "skill_acceleration_exec"
    assert skill_turbo.card.stateless is True
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
            side_effect=_isolated_session_side_effect(turbo_session),
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
            side_effect=_isolated_session_side_effect(turbo_session),
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


@pytest.mark.asyncio
async def test_tool_resume_restores_model_before_building_skill_turbo(
    _skill_turbo_runtime,
) -> None:
    adapter, turbo_session = _skill_turbo_runtime
    adapter.current_model = "answer-model"
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {
            "query": _QUERY,
            "metadata": {
                SKILL_TURBO_MODEL_IDENTITY_KEY: {
                    "model_name": "saved-model",
                    "model_ref": "saved-model-ref",
                }
            },
        },
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }

    def restore_model(_ctx):
        adapter.current_model = "saved-model"

    adapter.restore_skill_turbo_model_from_resume_ctx.side_effect = restore_model
    adapter.build_skill_turbo_config.side_effect = lambda: {
        "model_client": adapter.current_model
    }
    fake_turbo = _fake_turbo()
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
            return_value={
                "session_id": "resume-sid",
                "request_id": "req-resume",
                "channel_id": "web",
            },
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
            return_value=fake_turbo,
        ) as skill_turbo_cls,
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            side_effect=_isolated_session_side_effect(turbo_session),
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
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is True
    adapter.restore_skill_turbo_model_from_resume_ctx.assert_called_once_with(
        resume_ctx
    )
    adapter.build_skill_turbo_config.assert_called_once_with()
    skill_turbo_cls.assert_called_once_with({"model_client": "saved-model"})


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
            side_effect=_isolated_session_side_effect(turbo_session),
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
            _SKILL_TURBO_HITL_RESULT_KEY,
            set_skill_turbo_hitl_tic,
        )

        set_skill_turbo_hitl_tic(None)
        try:
            result = await skill_turbo.invoke({"query": _QUERY})
        finally:
            set_skill_turbo_hitl_tic(None)

    assert isinstance(result, dict)
    assert result.get(_SKILL_TURBO_HITL_RESULT_KEY) is True
    assert isinstance(result.get("request"), dict)
    clear_ctx.assert_not_awaited()
    turbo_session.post_run.assert_not_awaited()
    turbo_session.close_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_resume_answer_returns_clean_placeholder_without_stop_hint(
    _skill_turbo_runtime,
) -> None:
    """重复作答（resume_in_flight 命中）提前返回不得追加 NEUTRAL 停止提示。

    复现检视意见：duplicate 分支的 SkillTurbo 是 fresh 实例
    （artifact_holder 为空），若走 _wrap_skill_turbo_result 必然落入 NEUTRAL
    分支，追加"任务已结束…不要声称文件已发送"——与"任务正在处理中"语义
    矛盾：原始请求仍在执行、稍后交付，B 轮 LLM 会据此向用户宣告交付失败。
    且返回不得携带 success 键：帧级 success=true 会触发上层应用侧
    isSuccessfulPptTurboToolResult 结构匹配，误判 PPT turbo 成功并替换
    最终正文。同时锁定防重三件套：判重命中不 mark、不误清原始请求的
    in-flight 标志（_did_mark_in_flight 守卫）、不启动第二次 resume_stream。
    """
    adapter, turbo_session = _skill_turbo_runtime
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
        "resume_in_flight": True,
    }
    mark_in_flight = AsyncMock()
    clear_in_flight = AsyncMock()
    fake_turbo = _fake_turbo()
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
            return_value=fake_turbo,
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            side_effect=_isolated_session_side_effect(turbo_session),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.mark_resume_in_flight",
            mark_in_flight,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_in_flight",
            clear_in_flight,
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert "success" not in result
    text = str(result.get("result") or "")
    assert "任务正在处理中" in text
    # 不得出现任何"已结束 / 未确认交付"停止提示（NEUTRAL / CONFIRMED）
    assert "[SYSTEM]" not in text
    assert "任务已结束" not in text
    assert "不要声称文件已发送" not in text
    # 帧级守护：无 success 键的纯文本占位 dict → _infer_tool_result_error
    # 判 None → chat.tool_result 帧不带 success 字段，上层应用侧
    # isSuccessfulPptTurboToolResult（结构匹配 success===true）不会把
    # 占位帧误判成 PPT turbo 成功
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        _infer_tool_result_error,
    )

    assert _infer_tool_result_error(result) is None
    mark_in_flight.assert_not_awaited()
    clear_in_flight.assert_not_awaited()
    fake_turbo.resume_stream.assert_not_called()
    turbo_session.post_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_resume_marks_and_clears_in_flight_once(
    _skill_turbo_runtime,
) -> None:
    """正常 resume：mark 一次，finally 清一次（_did_mark_in_flight 守卫下）。"""
    adapter, turbo_session = _skill_turbo_runtime
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    mark_in_flight = AsyncMock()
    clear_in_flight = AsyncMock()
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
            side_effect=_isolated_session_side_effect(turbo_session),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.mark_resume_in_flight",
            mark_in_flight,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_in_flight",
            clear_in_flight,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_ctx",
            AsyncMock(),
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is True
    mark_in_flight.assert_awaited_once_with(turbo_session, resume_ctx)
    clear_in_flight.assert_awaited_once_with(turbo_session)


@pytest.mark.asyncio
async def test_hitl_abort_clears_in_flight_but_keeps_checkpoint(
    _skill_turbo_runtime,
) -> None:
    """resume 内二次中断：清 in-flight（防对断点的下次作答被误判重复）但保留新断点。

    锁定 finally 语义：_did_mark_in_flight=True 时 AbortError/TIC 路径仍须
    clear_resume_in_flight；release_checkpoint=False 时不得 clear_resume_ctx /
    post_run（新断点由 executor 落盘，供再下一次作答恢复）。
    """
    adapter, turbo_session = _skill_turbo_runtime
    turbo_session.close_stream = AsyncMock()
    resume_ctx = {
        "plan_code": "plan()",
        "inputs": {"query": _QUERY},
        "pending_tool_call_id": "tc-1",
        "task_states": [],
    }
    tic = SimpleNamespace(tool_call=SimpleNamespace(id="tc-ask"))
    mark_in_flight = AsyncMock()
    clear_in_flight = AsyncMock()
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
            side_effect=_isolated_session_side_effect(turbo_session),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=resume_ctx),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.mark_resume_in_flight",
            mark_in_flight,
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.clear_resume_in_flight",
            clear_in_flight,
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
            set_skill_turbo_hitl_tic,
        )

        set_skill_turbo_hitl_tic(None)
        try:
            result = await skill_turbo.invoke({"query": _QUERY})
        finally:
            set_skill_turbo_hitl_tic(None)

    mark_in_flight.assert_awaited_once_with(turbo_session, resume_ctx)
    clear_in_flight.assert_awaited_once_with(turbo_session)
    clear_ctx.assert_not_awaited()
    turbo_session.post_run.assert_not_awaited()


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
            side_effect=_isolated_session_side_effect(turbo_session),
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
            side_effect=_isolated_session_side_effect(turbo_session),
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
            side_effect=_isolated_session_side_effect(turbo_session),
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
async def test_skill_turbo_returns_failure_when_p10_delivery_failed(
    _skill_turbo_runtime,
) -> None:
    """P10 delivery_status=failed 时，不得再返回 success=True「任务已完成」。"""
    adapter, turbo_session = _skill_turbo_runtime
    turbo = _fake_turbo()
    turbo.artifact_holder = {
        "p10_delivery": {
            "info": {
                "delivery_status": "failed",
                "task_completed": False,
                "send_file_status": "skipped",
            }
        }
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
            return_value=None,
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
            return_value=turbo,
        ),
        patch(
            "openjiuwen.core.session.agent.create_agent_session",
            side_effect=_isolated_session_side_effect(turbo_session),
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.set_skill_turbo_id",
        ),
        patch(
            "jiuwenswarm.server.runtime.skill_turbo.permission_bridge.load_resume_ctx",
            AsyncMock(return_value=None),
        ),
    ):
        result = await skill_turbo.invoke({"query": _QUERY})

    assert result.get("success") is False
    assert "PPT 生成失败" in str(result.get("error") or "")
    assert "任务已完成" not in str(result.get("result") or "")
    turbo.run_stream.assert_called_once()


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


# ── B_03：taskProgress 槽位泄漏清算契约 ──
# 复现链：入口 gate 误判 forwarded（同轮并行 todo_create + skill_acceleration_exec
# 竞态）→ turbo Stage 快照顶掉外层 todo 槽位 → 该轮终态前被打断（HITL）→
# 续跑入口判 skip，终态快照被抑制 → 槽位永驻 in_progress（"Stage 卡住"）。


@pytest.mark.asyncio
async def test_skip_mode_terminal_settles_leaked_task_slot(
    _skill_turbo_runtime,
) -> None:
    """B_03 复现：登记遗留（首入口误判 forwarded）+ skip 续跑入口终止性出口
    → 终态快照必须补发关账（FIFO+OOB）；task.start/complete 仍禁。"""
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-leak",
    )
    send_push = AsyncMock(return_value=1)
    push_server = SimpleNamespace(send_push=send_push)

    # 前史：首入口误判 forwarded，非终态快照已顶掉槽位（登记未闭合）
    task_slot_guard.claim_task_slot("sess-leak", {"task_deadbeef", "task_stage13"})

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(payload={
            "event_type": "task.update",
            "tasks": [
                {"task_id": "task_deadbeef", "task_content": "Stage 1", "status": "completed"},
                {"task_id": "task_stage13", "task_content": "Stage 13: 交付", "status": "completed"},
            ],
        })
        yield SimpleNamespace(payload={
            "event_type": "task.start",
            "task_id": "task_stage13",
            "task_content": "Stage 13: 交付",
        })
        yield SimpleNamespace(payload={
            "event_type": "chat.delta",
            "content": "交付完成",
            "task_id": "task_stage13",
        })

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(True)
    try:
        with _forwarding_env(
            adapter,
            turbo,
            parent_session,
            metadata={
                "request_id": "req-settle",
                "channel_id": "test-channel",
                "session_id": "sess-ext",
            },
            push_server=push_server,
        ):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    assert result.get("success") is True
    forwarded = [
        call.args[0].payload
        for call in parent_session.write_stream.await_args_list
    ]
    task_updates = [
        p for p in forwarded if p.get("event_type") == "task.update"
    ]
    # 全部流内快照被 skip 抑制，仅清算补发一帧终态快照
    assert len(task_updates) == 1
    assert all(t["status"] == "completed" for t in task_updates[0]["tasks"])
    # task.start 不得放行（taskStack 归段保护——skip 语义不因清算破例）
    assert all(p.get("event_type") != "task.start" for p in forwarded)
    # 登记簿已注销；OOB 直推同步发出（右侧任务列表由 OOB 驱动）
    assert task_slot_guard.peek_leaked_task_slot("sess-leak") is None
    assert send_push.await_count == 1
    pushed = send_push.await_args.args[0]
    assert pushed["payload"]["event_type"] == "task.update"
    assert all(t["status"] == "completed" for t in pushed["payload"]["tasks"])


@pytest.mark.asyncio
async def test_skip_mode_without_leak_keeps_slot_untouched(
    _skill_turbo_runtime,
) -> None:
    """正常 skip 路径（无登记）不得因清算放行任何 task.*——否则外层 todo 槽位
    被 turbo 已完成清单顶掉（skip 机制要防的回归，B_03 关键副作用守卫）。"""
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-clean",
    )

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(payload={
            "event_type": "task.update",
            "tasks": [
                {"task_id": "task_a", "task_content": "Stage 1", "status": "completed"},
            ],
        })
        yield SimpleNamespace(payload={
            "event_type": "chat.delta",
            "content": "完成",
        })

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(True)
    try:
        with _forwarding_env(adapter, turbo, parent_session):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    assert result.get("success") is True
    forwarded = [
        call.args[0].payload
        for call in parent_session.write_stream.await_args_list
    ]
    assert [p.get("event_type") for p in forwarded] == ["chat.delta"]
    assert task_slot_guard.peek_leaked_task_slot("sess-clean") is None


@pytest.mark.asyncio
async def test_forwarded_run_closes_own_slot_on_terminal_snapshot(
    _skill_turbo_runtime,
) -> None:
    """转发分支自己跑到终态：非终态快照登记、终态快照即行注销——后续
    skip 入口不误清算（流内断言锁定两阶段转移）。"""
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-fwd",
    )

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(payload={
            "event_type": "task.update",
            "tasks": [
                {"task_id": "task_a", "task_content": "Stage 1", "status": "in_progress"},
            ],
        })
        # 非终态快照已转发：登记建立
        assert task_slot_guard.peek_leaked_task_slot("sess-fwd") == frozenset({"task_a"})
        yield SimpleNamespace(payload={
            "event_type": "task.update",
            "tasks": [
                {"task_id": "task_a", "task_content": "Stage 1", "status": "completed"},
            ],
        })
        # 终态快照转发后：登记注销（任务集自然闭合）
        assert task_slot_guard.peek_leaked_task_slot("sess-fwd") is None
        yield SimpleNamespace(payload={
            "event_type": "chat.delta",
            "content": "完成",
        })

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(False)
    try:
        with _forwarding_env(adapter, turbo, parent_session):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    assert result.get("success") is True
    assert task_slot_guard.peek_leaked_task_slot("sess-fwd") is None


@pytest.mark.asyncio
async def test_forwarded_interrupt_leaves_slot_claimed(
    _skill_turbo_runtime,
) -> None:
    """转发分支终态前被打断（AbortError 暂停语义）：登记保留——skip 续跑
    入口的终态清算依赖它（登记跨请求存续是登记簿的存在意义）。"""
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-fwd",
    )

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(payload={
            "event_type": "task.update",
            "tasks": [
                {"task_id": "task_deadbeef", "task_content": "Stage 4", "status": "in_progress"},
            ],
        })
        raise AbortError("paused")

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(False)
    try:
        with _forwarding_env(adapter, turbo, parent_session):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    # AbortError 无 TIC：按中断错误收口（非暂停卡路径，但同样不清算）
    assert result.get("success") is False
    assert task_slot_guard.peek_leaked_task_slot("sess-fwd") == frozenset(
        {"task_deadbeef"}
    )


@pytest.mark.asyncio
async def test_skip_mode_abort_keeps_leak_for_resume(
    _skill_turbo_runtime,
) -> None:
    """skip 入口被打断（HITL 暂停族路径）：不得清算——任务集还要续跑，
    此时关账等于把 in_progress 谎报成终态（S_08 暂停态禁终态帧的同族约束）。"""
    adapter, _turbo_session = _skill_turbo_runtime
    parent_session = SimpleNamespace(
        write_stream=AsyncMock(),
        get_session_id=lambda: "sess-leak",
    )

    task_slot_guard.claim_task_slot("sess-leak", {"task_deadbeef"})

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(payload={
            "event_type": "task.update",
            "tasks": [
                {"task_id": "task_deadbeef", "task_content": "Stage 4", "status": "in_progress"},
            ],
        })
        raise AbortError("paused")

    turbo = _fake_turbo()
    turbo.run_stream = MagicMock(side_effect=stream)
    token = set_skill_turbo_outer_todo_active(True)
    try:
        with _forwarding_env(adapter, turbo, parent_session):
            result = await skill_turbo.invoke({"query": _QUERY})
    finally:
        reset_skill_turbo_outer_todo_active(token)

    assert result.get("success") is False
    forwarded = [
        call.args[0].payload
        for call in parent_session.write_stream.await_args_list
    ]
    assert all(p.get("event_type") != "task.update" for p in forwarded)
    # 登记保留：resume 续跑入口的终止性出口仍可关账
    assert task_slot_guard.peek_leaked_task_slot("sess-leak") == frozenset(
        {"task_deadbeef"}
    )
