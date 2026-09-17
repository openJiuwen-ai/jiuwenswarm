# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for CodeAgentModeRail plan-mode enforcement."""

# pylint: disable=protected-access

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.code.prompt.plan_approval import PLAN_EXECUTE_CTX_KEY
from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import CodeAgentModeRail


@pytest.mark.asyncio
async def test_before_tool_call_blocks_switch_mode_exit_in_plan_mode() -> None:
    rail = CodeAgentModeRail(allowed_tools=["switch_mode"])
    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="test-plan")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    rail._agent = agent

    parent = AsyncMock()
    with patch.object(CodeAgentModeRail.__bases__[0], "before_tool_call", parent):
        ctx = SimpleNamespace(
            session=SimpleNamespace(),
            inputs=SimpleNamespace(
                tool_name="switch_mode",
                tool_call=SimpleNamespace(
                    id="call_1",
                    arguments='{"mode": "normal"}',
                ),
                tool_args={"mode": "normal"},
            ),
            extra={},
        )
        await rail.before_tool_call(ctx)

    parent.assert_not_awaited()
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_before_tool_call_blocks_non_git_write_in_plan_mode() -> None:
    rail = CodeAgentModeRail(allowed_tools=["bash"])
    agent = MagicMock()
    plan_state = SimpleNamespace(mode="plan", plan_slug="test-plan")
    agent.load_state.return_value = SimpleNamespace(plan_mode=plan_state)
    rail._agent = agent

    parent = AsyncMock()
    with patch.object(CodeAgentModeRail.__bases__[0], "before_tool_call", parent):
        ctx = SimpleNamespace(
            session=SimpleNamespace(),
            inputs=SimpleNamespace(
                tool_name="bash",
                tool_call=SimpleNamespace(id="call_1"),
                tool_args={"command": "mkdir -p src/foo"},
            ),
            extra={},
        )
        await rail.before_tool_call(ctx)

    parent.assert_awaited_once()
    assert ctx.extra.get("_skip_tool") is True


_EXIT_NOTIFICATION = "<system-reminder>\nStart executing the first step now.\n</system-reminder>"


def _exit_plan_ctx(
    tool_result,
    *,
    rejected: bool = False,
    defer_execution: bool = False,
):
    """Build an after_tool_call ctx for an exit_plan_mode call."""
    extra = {}
    if rejected:
        extra["_plan_rejected"] = True
    if defer_execution:
        extra[PLAN_EXECUTE_CTX_KEY] = True
    return SimpleNamespace(
        session=SimpleNamespace(),
        inputs=SimpleNamespace(
            tool_name="exit_plan_mode",
            tool_call=SimpleNamespace(id="call_1"),
            tool_args={},
            tool_result=tool_result,
            tool_msg=SimpleNamespace(content=tool_result),
        ),
        extra=extra,
        request_force_finish=MagicMock(),
    )


def _exit_plan_rail(plan_mode: str = "plan"):
    rail = CodeAgentModeRail(allowed_tools=["exit_plan_mode"])
    agent = MagicMock()
    agent.load_state.return_value = SimpleNamespace(
        plan_mode=SimpleNamespace(mode=plan_mode, plan_slug="test-plan")
    )
    rail._agent = agent
    rail._unregister_task_tool = MagicMock()
    return rail, agent


@pytest.mark.asyncio
async def test_after_tool_call_drops_echoed_plan_body() -> None:
    """The tool echoes the whole plan back; only the header + reminder survive.

    Leaving 10k+ chars of plan in the tool_result buries the reminder and the
    model answers with a summary of the plan instead of executing it.
    """
    plan_body = "# Login module\n\n" + ("step detail\n" * 500)
    result = (
        "Plan mode ended. \nPlan file: /tmp/plans/x.md\n\n"
        f"## Plan:\n{plan_body}\n\n{_EXIT_NOTIFICATION}"
    )
    rail, _ = _exit_plan_rail(plan_mode="normal")
    ctx = _exit_plan_ctx(result)

    await rail.after_tool_call(ctx)

    compacted = ctx.inputs.tool_result
    assert plan_body not in compacted
    assert "Plan file: /tmp/plans/x.md" in compacted
    assert compacted.endswith(_EXIT_NOTIFICATION)
    # The ToolMessage is what actually reaches the LLM, so it must match.
    assert ctx.inputs.tool_msg.content == compacted


@pytest.mark.asyncio
async def test_after_tool_call_keeps_empty_plan_result_intact() -> None:
    """No plan body to strip — the result must pass through untouched."""
    result = f"Plan mode ended. You can now exit the turn.\nPlan file: x.md\n\n{_EXIT_NOTIFICATION}"
    rail, _ = _exit_plan_rail(plan_mode="normal")
    ctx = _exit_plan_ctx(result)

    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_result == result


@pytest.mark.asyncio
async def test_after_tool_call_skips_teardown_while_awaiting_approval() -> None:
    """The approval interrupt fires this hook before the tool body ran.

    Restoring the mode here would drop the user out of plan mode before they
    answered, which breaks "skip" (it must keep the session in plan mode).
    """
    rail, agent = _exit_plan_rail(plan_mode="plan")
    ctx = _exit_plan_ctx({"interrupt": "pending approval"})

    await rail.after_tool_call(ctx)

    agent.restore_mode_after_plan_exit.assert_not_called()
    rail._unregister_task_tool.assert_not_called()


@pytest.mark.asyncio
async def test_after_tool_call_finishes_turn_for_web_deferred_execution() -> None:
    """Web 的执行分两轮：本轮只退出 plan，不让模型继续跑。"""
    result = (
        "Plan mode ended. \nPlan file: /tmp/plans/x.md\n\n"
        f"## Plan:\nbody\n\n{_EXIT_NOTIFICATION}"
    )
    rail, _ = _exit_plan_rail(plan_mode="normal")
    ctx = _exit_plan_ctx(result, defer_execution=True)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


@pytest.mark.asyncio
async def test_after_tool_call_keeps_tui_turn_running_after_approve() -> None:
    """TUI 不带该标记，批准后必须留在同一轮继续实现。"""
    result = (
        "Plan mode ended. \nPlan file: /tmp/plans/x.md\n\n"
        f"## Plan:\nbody\n\n{_EXIT_NOTIFICATION}"
    )
    rail, _ = _exit_plan_rail(plan_mode="normal")
    ctx = _exit_plan_ctx(result)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_after_tool_call_restores_mode_when_tool_left_it_in_plan() -> None:
    """Empty-plan path: invoke() returns early without restoring the mode."""
    rail, agent = _exit_plan_rail(plan_mode="plan")
    ctx = _exit_plan_ctx("Plan mode ended. You can now exit the turn.\nPlan file: x.md")

    await rail.after_tool_call(ctx)

    agent.restore_mode_after_plan_exit.assert_called_once()
    rail._unregister_task_tool.assert_called_once()


def test_init_no_longer_patches_exit_plan_mode_invoke() -> None:
    """After removing the pending-approval pattern, CodeAgentModeRail.init()
    should NOT patch exit_plan_mode.invoke. The parent AgentModeRail's
    ExitPlanModeTool handles mode restoration directly inside invoke().
    """
    rail = CodeAgentModeRail(allowed_tools=["exit_plan_mode"])
    tool = MagicMock()
    original_invoke = object()
    tool.invoke = original_invoke
    tool.card.name = "exit_plan_mode"
    tool._language = "cn"
    rail._tools = [tool]

    agent = MagicMock()
    rail.init(agent)

    # Verify the tool's invoke was NOT replaced
    assert tool.invoke is original_invoke
