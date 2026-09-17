# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PlanApprovalInterruptRail reject feedback wrapping."""

from types import SimpleNamespace

import pytest
from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, RejectResult

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_EXECUTE_CTX_KEY,
    wrap_plan_revision_feedback,
)
from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
    PlanApprovalInterruptRail,
)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        extra={},
        session=None,
        request_force_finish=lambda _payload: None,
    )


def _tool_call() -> SimpleNamespace:
    return SimpleNamespace(name="exit_plan_mode", arguments="{}")


def test_wrap_plan_revision_feedback_keeps_user_text() -> None:
    wrapped = wrap_plan_revision_feedback("制作5页PPT", "cn")
    assert "尚未批准" in wrapped or "用户要求修订计划" in wrapped
    assert "exit_plan_mode" in wrapped
    assert "制作5页PPT" in wrapped


def test_wrap_plan_revision_feedback_keeps_braces_in_user_text() -> None:
    wrapped = wrap_plan_revision_feedback("改成 {name} 占位", "cn")
    assert "改成 {name} 占位" in wrapped
    assert "尚未批准" in wrapped or "用户要求修订计划" in wrapped


@pytest.mark.asyncio
async def test_revise_reject_wraps_feedback_injection() -> None:
    rail = PlanApprovalInterruptRail()
    ctx = _ctx()

    decision = await rail.resolve_interrupt(
        ctx,
        tool_call=_tool_call(),
        user_input={
            "approved": False,
            "auto_confirm": False,
            "plan_revise": True,
            "feedback": "制作5页PPT",
        },
    )

    assert isinstance(decision, RejectResult)
    text = str(decision.tool_result)
    assert "用户要求修订计划" in text
    assert "exit_plan_mode" in text
    assert "制作5页PPT" in text
    assert ctx.extra.get("_plan_rejected") is True


@pytest.mark.asyncio
async def test_skip_reject_does_not_wrap_revision_reminder() -> None:
    finished: list[object] = []
    ctx = SimpleNamespace(
        extra={},
        session=None,
        request_force_finish=lambda payload: finished.append(payload),
    )
    rail = PlanApprovalInterruptRail()

    decision = await rail.resolve_interrupt(
        ctx,
        tool_call=_tool_call(),
        user_input={
            "approved": False,
            "auto_confirm": False,
            "plan_skip": True,
            "feedback": "用户跳过了计划审批",
        },
    )

    assert isinstance(decision, RejectResult)
    assert "exit_plan_mode" not in str(decision.tool_result)
    assert ctx.extra.get("_plan_skipped") is True
    assert finished


@pytest.mark.asyncio
async def test_tui_reject_does_not_wrap_revision_reminder() -> None:
    rail = PlanApprovalInterruptRail()
    ctx = _ctx()

    decision = await rail.resolve_interrupt(
        ctx,
        tool_call=_tool_call(),
        user_input={
            "approved": False,
            "auto_confirm": False,
            "feedback": "用户拒绝",
        },
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == "用户拒绝"
    assert ctx.extra.get("_plan_rejected") is True


@pytest.mark.asyncio
async def test_plan_execute_approve_does_not_wrap_feedback() -> None:
    rail = PlanApprovalInterruptRail()
    ctx = _ctx()

    decision = await rail.resolve_interrupt(
        ctx,
        tool_call=_tool_call(),
        user_input={
            "approved": True,
            "auto_confirm": False,
            "plan_execute": True,
            "feedback": "",
        },
    )

    assert isinstance(decision, ApproveResult)
    assert ctx.extra.get(PLAN_EXECUTE_CTX_KEY) is True
    assert "_plan_rejected" not in ctx.extra
